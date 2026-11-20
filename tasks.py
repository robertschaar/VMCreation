from config import *
import os
import re
import time
import smtplib
import requests
import redis
from urllib.parse import quote
from email.message import EmailMessage
from celery import Celery
from proxmoxer import ProxmoxAPI

celery_app = Celery('tasks', broker='redis://localhost:6379/0', backend='redis://localhost:6379/0')
celery_app.conf.broker_transport_options = {'visibility_timeout': 14400}
celery_app.conf.task_acks_late = False

redis_client = redis.Redis.from_url('redis://localhost:6379/0')

SENDER_EMAIL = SMTP_USER

proxmox = ProxmoxAPI(PROXMOX_IP, user=TOKEN_USER, token_name=TOKEN_NAME, token_value=TOKEN_SECRET, verify_ssl=False)


def send_email_alert(subject, body):
    try:
        msg = EmailMessage()
        msg.set_content(body)
        msg['Subject'] = subject
        msg['From'] = SENDER_EMAIL
        msg['To'] = "robert.schaar@sunbirddcim.com"
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"[!] Failed to send email alert: {e}")



def update_freshdesk_ticket(ticket_id, message, private=False):
    if not ticket_id or not FRESHDESK_API_KEY:
        print(f"[!] Skipping Freshdesk update for #{ticket_id}: No API key configured.")
        return
    url = f"https://{FRESHDESK_DOMAIN}/api/v2/tickets/{ticket_id}/notes"
    try:
        requests.post(url, json={"body": message, "private": private}, auth=(FRESHDESK_API_KEY, "X"), timeout=10)
    except Exception as e:
        print(f"[!] Freshdesk API error: {e}")

def get_next_vmid():
    return int(proxmox.cluster.nextid.get())


def get_vm_node(vmid):
    for vm in proxmox.cluster.resources.get(type="vm"):
        if int(vm.get("vmid", -1)) == int(vmid):
            return vm["node"]
    raise ValueError(f"VM {vmid} not found in cluster.")


def get_best_node():
    nodes = proxmox.nodes.get()
    online_nodes = [n for n in nodes if n.get("status") == "online"]
    if not online_nodes:
        raise RuntimeError("No online Proxmox nodes available.")
    online_nodes.sort(key=lambda n: n.get("mem", 0) / max(n.get("maxmem", 1), 1))
    return online_nodes[0]["node"]


def find_template_vmid(name_prefix):
    normalized = name_prefix.lower().replace("_", "").replace("-", "").replace(".", "")
    for vm in proxmox.cluster.resources.get(type="vm"):
        if vm.get("template") == 1:
            template_name = vm.get("name", "").lower().replace("_", "").replace("-", "").replace(".", "")
            if normalized in template_name:
                return vm["vmid"]
    raise ValueError(f"No Proxmox template found matching prefix: '{name_prefix}'")


def sanitize_proxmox_name(raw_name):
    return re.sub(r"[^a-zA-Z0-9.-]+", "-", raw_name.strip()).strip("-")[:63]


def extract_target_ip(vm_name):
    ip_match = re.search(r"[-_\s](\d{1,3}\.\d{1,3})$", vm_name.strip())
    if not ip_match:
        raise ValueError(f"Could not parse IP from: '{vm_name}'")
    return f"10.34.{ip_match.group(1)}"


def parse_vm_name(vm_name):
    clean = vm_name.strip()
    prefix_match = re.match(r"^([a-zA-Z]+[-_\s]\d+\.\d+\.\d+)", clean)
    if not prefix_match:
        raise ValueError(f"Could not extract prefix from: '{clean}'")
    return find_template_vmid(re.sub(r"\s+", "-", prefix_match.group(1))), extract_target_ip(clean)


def wait_for_task(node, upid, timeout=1200):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = proxmox.nodes(node).tasks(upid).status.get()
        if status.get("status") == "stopped":
            if status.get("exitstatus") == "OK":
                return
            raise RuntimeError(f"Proxmox task failed: {status.get('exitstatus')}")
        time.sleep(3)
    raise TimeoutError("Proxmox task timed out")


def run_guest_command(vmid, command_list, timeout=3600):
    current_node = get_vm_node(vmid)
    pid = None
    for _ in range(12):
        try:
            if proxmox.nodes(current_node).qemu(vmid).status.current.get().get("status") == "running":
                result = proxmox.nodes(current_node).qemu(vmid).agent.exec.post(command=command_list)
                pid = result.get("pid")
                if pid is not None:
                    break
        except Exception:
            time.sleep(5)
    if pid is None:
        raise RuntimeError(f"QEMU Guest Agent did not respond on VM {vmid} ({current_node}).")

    start_time = time.time()
    while True:
        if time.time() - start_time > timeout:
            raise TimeoutError("Guest command timed out.")
        try:
            vm_state = proxmox.nodes(current_node).qemu(vmid).status.current.get().get("status")
            if vm_state != "running":
                raise RuntimeError(f"VM {vmid} on {current_node} stopped unexpectedly (state={vm_state}).")
            status = proxmox.nodes(current_node).qemu(vmid).agent.get("exec-status", pid=pid)
            if status.get("exited") == 1:
                if status.get("exitcode") != 0:
                    err_out = status.get("err-data", "") or status.get("out-data", "")
                    raise RuntimeError(f"Guest command failed (exit {status.get('exitcode')}): {err_out or status}")
                return status
        except Exception as e:
            if "stopped unexpectedly" in str(e) or "Guest command failed" in str(e):
                raise
        time.sleep(5)


@celery_app.task(bind=True)
def deploy_vm_task(self, form_data):
    vm_name = (form_data.get("vm_name") or "Unknown").strip()
    safe_vm_name = sanitize_proxmox_name(vm_name)
    lock_key = f"vmcreator:lock:{safe_vm_name}"

    if not redis_client.set(lock_key, self.request.id or "1", nx=True, ex=14400):
        print(f"[!] Duplicate task ignored for '{safe_vm_name}' (already running).")
        return f"Skipped duplicate deployment for {safe_vm_name} (already in progress)."

    try:
        storage_type = form_data.get("storage_type")
        ftp_user = (form_data.get("ftp_user") or "").strip()
        ftp_pass = (form_data.get("ftp_pass") or "").strip()
        backup_file = (form_data.get("backup_file") or "").strip()
        template_override = (form_data.get("template_vmid") or "").strip()
        pool_override = (form_data.get("pool_id") or "").strip()
        ticket_id = (form_data.get("ticket_id") or "").strip()

        if not backup_file:
            raise ValueError("No backup file was selected.")

        if template_override and template_override.lower() not in ("none", "null", "undefined"):
            template_vmid = int(template_override)
            new_ip = extract_target_ip(vm_name)
        else:
            template_vmid, new_ip = parse_vm_name(vm_name)

        for vm in proxmox.cluster.resources.get(type="vm"):
            if vm.get("template") != 1 and vm.get("name", "").lower() == safe_vm_name.lower():
                raise ValueError(
                    f"VM '{safe_vm_name}' already exists in Proxmox (VMID {vm.get('vmid')} on {vm.get('node')}). Delete it first before redeploying."
                )

        new_vmid = get_next_vmid()
        template_node = get_vm_node(template_vmid)
        target_node = get_best_node()

        clone_params = {"newid": new_vmid, "name": safe_vm_name, "full": 1, "storage": STORAGE_POOL}
        if pool_override:
            clone_params["pool"] = pool_override

        task_upid = proxmox.nodes(template_node).qemu(template_vmid).clone.post(**clone_params)
        wait_for_task(template_node, task_upid)

        if template_node != target_node:
            migrate_upid = proxmox.nodes(template_node).qemu(new_vmid).migrate.post(
                target=target_node, **{"with-local-disks": 1}
            )
            wait_for_task(template_node, migrate_upid)
            current_node = target_node
        else:
            current_node = template_node

        proxmox.nodes(current_node).qemu(new_vmid).config.post(
            ipconfig0=f"ip={new_ip}/{NETMASK_CIDR},gw={GATEWAY}",
            nameserver=NAMESERVER,
            ciuser="sunbird",
            cipassword="PlumP0wderR0X",
        )
        start_upid = proxmox.nodes(current_node).qemu(new_vmid).status.start.post()
        wait_for_task(current_node, start_upid, timeout=120)

        for _ in range(40):
            try:
                if new_ip in str(proxmox.nodes(current_node).qemu(new_vmid).agent.get("network-get-interfaces")):
                    time.sleep(10)
                    break
            except Exception:
                time.sleep(5)

        run_guest_command(
            new_vmid,
            ["bash", "-c", "(sleep 1 && systemctl restart qemu-guest-agent) >/dev/null 2>&1 &"],
        )
        time.sleep(8)

        run_guest_command(
            new_vmid,
            [
                "bash",
                "-lc",
                'curl -u "tools:EX2PCdfa" -sS -f -o /home/sunbird/dev_restore "ftp://10.34.0.41/vcenter/dev_restore" && chmod +x /home/sunbird/dev_restore',
            ],
        )

        safe_archive_name = os.path.basename(backup_file)
        guest_archive_path = f"/home/sunbird/{safe_archive_name}"

        if storage_type == "cold":
            cifs_cmd = [
                "bash",
                "-lc",
                f"trap 'umount -l /mnt/coldstorage 2>/dev/null || true' EXIT; "
                f"mkdir -p /mnt/coldstorage && "
                f"mounted=0; for i in {{1..5}}; do "
                f"mount -t cifs '{COLD_STORAGE_SHARE}' /mnt/coldstorage -o username='backup',password='Sun2015*',vers=3.0 && mounted=1 && break; "
                f"sleep 5; done; "
                f"[ $mounted -eq 1 ] || {{ echo 'Failed to mount Cold Storage after 5 attempts.' >&2; exit 1; }}; "
                f"src='/mnt/coldstorage/{vm_name}/{backup_file}'; "
                f"cp \"$src\" '{guest_archive_path}' && "
                f"[ -s '{guest_archive_path}' ] && "
                f"[ \"$(stat -c%s \"$src\")\" = \"$(stat -c%s '{guest_archive_path}')\" ]",
            ]
            run_guest_command(new_vmid, cifs_cmd)
        else:
            encoded_path = "/".join(quote(part) for part in backup_file.lstrip("/").split("/"))
            download_cmd = [
                "bash",
                "-lc",
                f"ok=0; for i in {{1..10}}; do "
                f"curl -sS -f --globoff -u '{ftp_user}:{ftp_pass}' -C - 'ftp://{FTP_SERVER_IP}/{encoded_path}' -o '{guest_archive_path}' 2>/home/sunbird/download_debug.log && ok=1 && break; "
                f"sleep 10; done; "
                f"if [ $ok -ne 1 ] || [ ! -s '{guest_archive_path}' ]; then cat /home/sunbird/download_debug.log >&2; exit 1; fi",
            ]
            run_guest_command(new_vmid, download_cmd)

        verify_cmd = [
            "bash",
            "-lc",
            f"if file '{guest_archive_path}' | grep -q 'gzip compressed'; then "
            f"gzip -t '{guest_archive_path}' || {{ echo 'Backup archive is corrupted or truncated (gzip -t failed).' >&2; exit 1; }}; "
            f"fi",
        ]
        run_guest_command(new_vmid, verify_cmd)

        restore_cmd = [
            "bash",
            "-lc",
            f"source /etc/profile 2>/dev/null || true; [ -z \"$JAVA_HOME\" ] && command -v java >/dev/null 2>&1 && export JAVA_HOME=$(dirname $(dirname $(readlink -f $(command -v java)))); "
            f"timeout 180 systemctl is-system-running --wait >/dev/null 2>&1 || true; "
            f"sleep 45; "
            f"cd /home/sunbird && "
            f"echo '=== Restore Output ({safe_archive_name}) ===' > /home/sunbird/restore_debug.log && "
            f"/home/sunbird/dev_restore '{guest_archive_path}' >> /home/sunbird/restore_debug.log 2>&1; "
            f"if grep -q 'Unable to stop all the required services' /home/sunbird/restore_debug.log; then "
            f"cp /home/sunbird/restore_debug.log /home/sunbird/restore_attempt1.log; "
            f"sleep 30; "
            f"echo '=== Retry Restore Output ({safe_archive_name}) ===' > /home/sunbird/restore_debug.log && "
            f"/home/sunbird/dev_restore '{guest_archive_path}' >> /home/sunbird/restore_debug.log 2>&1; "
            f"fi; "
            f"if grep -Eq 'at /usr/local/sbin/restore\\.pl line 3190|An error occurred|Unable to stop all the required services|Restore failed|ERROR:.*restore' /home/sunbird/restore_debug.log; then "
            f"tail -n 40 /home/sunbird/restore_debug.log >&2; exit 1; fi; "
            f"rm -f '{guest_archive_path}'",
        ]
        run_guest_command(new_vmid, restore_cmd, timeout=10800)

        final_node = get_vm_node(new_vmid)
        try:
            proxmox.nodes(final_node).qemu(new_vmid).status.reboot.post()
            time.sleep(15)
            proxmox.cluster.ha.resources.post(sid=f"vm:{new_vmid}", state="started")
        except Exception:
            pass

        redis_client.expire(lock_key, 120)
        success_msg = f"VM {safe_vm_name} deployed successfully on {new_ip}."
        send_email_alert(f"Success: VM Deployment {safe_vm_name}", success_msg)
        if ticket_id:
            fd_msg = f'VM {safe_vm_name} has been deployed successfully at <a href="https://{new_ip}">https://{new_ip}</a>.'
            update_freshdesk_ticket(ticket_id, fd_msg, private=True)
        return success_msg

    except Exception as e:
        redis_client.delete(lock_key)
        error_msg = f"Deployment failed for VM: '{vm_name}'.\n\nError details:\n{str(e)}"
        send_email_alert(f"FAILED: VM Deployment {vm_name}", error_msg)
        if ticket_id:
            update_freshdesk_ticket(ticket_id, f"<pre>{error_msg}</pre>", private=True)
        raise e
