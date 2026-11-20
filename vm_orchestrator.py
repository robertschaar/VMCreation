from config import *
import ftplib
import re
import time
import os
import json
import smtplib
import base64
import subprocess
import urllib.request
import urllib.error
from email.message import EmailMessage
from flask import Flask, jsonify, render_template, request
from proxmoxer import ProxmoxAPI

app = Flask(__name__)

# --- Configuration (Defaults to previous static values, but checks env vars first) ---
SENDER_EMAIL = SMTP_USER
# ---------------------

proxmox = ProxmoxAPI(
    PROXMOX_IP,
    user=TOKEN_USER,
    token_name=TOKEN_NAME,
    token_value=TOKEN_SECRET,
    verify_ssl=False,
)

def send_completion_email(to_email, vm_name, new_ip):
    try:
        msg = EmailMessage()
        msg.set_content(
            f"Hello,\n\n"
            f"Your requested VM '{vm_name}' has been successfully provisioned, restored, and secured.\n\n"
            f"You can access it at: https://{new_ip}\n\n"
            f"Thank you,\nAutomation Team"
        )
        msg['Subject'] = f"VM Provisioning Complete: {vm_name}"
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email

        s = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
        s.quit()
        print(f"  Completion email sent to {to_email}")
    except Exception as e:
        print(f"  Failed to send email to {to_email}. Error: {e}")

def add_freshdesk_note(ticket_id, vm_name, new_ip, new_vmid, node_name):
    if not FRESHDESK_API_KEY:
        msg = "FD_API_KEY not set; skipped Freshdesk update."
        print(f"  [-] Warning: {msg}")
        return False, msg

    url = f"https://{FRESHDESK_DOMAIN}/api/v2/tickets/{ticket_id}/notes"
    vm_url = f"https://{new_ip}"

    body = (
        f"Hello,<br><br>"
        f"The virtual machine <strong>{vm_name}</strong> (VMID: {new_vmid} on node <code>{node_name}</code>) "
        f"has been successfully provisioned and restored in Proxmox.<br><br>"
        f"<strong>VM Link:</strong> <a href=\"{vm_url}\">{vm_url}</a><br><br>"
        f"Automated VM Deployment System"
    )

    data = json.dumps({"body": body, "private": True}).encode("utf-8")
    auth_string = f"{FRESHDESK_API_KEY}:X"
    auth_bytes = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {auth_bytes}")

    try:
        print(f"  [*] Adding private note to Freshdesk ticket #{ticket_id}...")
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.getcode() == 201:
                print(f"  [+] Freshdesk ticket #{ticket_id} updated successfully.")
                return True, f"Updated Ticket #{ticket_id}"
            else:
                msg = f"Unexpected HTTP status {response.getcode()}"
                print(f"  [-] {msg}")
                return False, msg
    except urllib.error.HTTPError as e:
        msg = f"HTTP Error {e.code} - {e.reason}"
        print(f"  [-] Failed to update Freshdesk: {msg}")
        return False, msg
    except Exception as e:
        msg = str(e)
        print(f"  [-] Failed to update Freshdesk: {msg}")
        return False, msg

def get_next_vmid():
    return int(proxmox.cluster.nextid.get())

def get_vm_node(vmid):
    vms = proxmox.cluster.resources.get(type="vm")
    for vm in vms:
        if int(vm.get("vmid", -1)) == int(vmid):
            return vm["node"]
    raise ValueError(f"VM {vmid} not found anywhere in the cluster.")

def find_template_vmid(name_prefix):
    normalized_prefix = name_prefix.lower().replace("_", "").replace("-", "")
    vms = proxmox.cluster.resources.get(type="vm")
    for vm in vms:
        if vm.get("template") == 1:
            template_name = vm.get("name", "").lower().replace("_", "").replace("-", "")
            if normalized_prefix in template_name:
                return vm["vmid"]
    raise ValueError(f"No Proxmox template found matching prefix: '{name_prefix}'")

def sanitize_proxmox_name(raw_name):
    # Proxmox clones require strict [a-zA-Z0-9.-] names
    sanitized = re.sub(r"[^a-zA-Z0-9.-]+", "-", raw_name.strip()).strip("-")
    return sanitized[:63]

def extract_target_ip(vm_name):
    # Extracts target IP even if preceded by spaces, hyphens, or underscores
    ip_match = re.search(r"[-_\s](\d{1,3}\.\d{1,3})$", vm_name.strip())
    if not ip_match:
        raise ValueError(f"Could not parse trailing IP octets from VM name: '{vm_name}'")
    return f"10.34.{ip_match.group(1)}"

def parse_vm_name(vm_name):
    clean_name = vm_name.strip()
    target_ip = extract_target_ip(clean_name)

    # Allow spaces, underscores, or hyphens after prefix
    prefix_match = re.match(r"^([a-zA-Z]+[-_\s]\d+\.\d+\.\d+)", clean_name)
    if not prefix_match:
        raise ValueError(f"Could not extract appliance prefix from VM name: '{clean_name}'")
    
    appliance_prefix = re.sub(r"\s+", "-", prefix_match.group(1))
    template_vmid = find_template_vmid(appliance_prefix)
    return template_vmid, target_ip

def wait_for_task(node, upid, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = proxmox.nodes(node).tasks(upid).status.get()
        if status.get("status") == "stopped":
            if status.get("exitstatus") == "OK":
                return
            raise RuntimeError(f"Proxmox task {upid} failed: {status.get('exitstatus')}")
        time.sleep(3)
    raise TimeoutError(f"Timed out waiting for Proxmox task {upid}")

def run_guest_command(vmid, command_list, retries=6, retry_delay=5, timeout=3600):
    for attempt in range(1, retries + 1):
        try:
            current_node = get_vm_node(vmid)
            vm_status = proxmox.nodes(current_node).qemu(vmid).status.current.get()
            if vm_status.get("status") != "running":
                raise RuntimeError(f"VM {vmid} state is '{vm_status.get('status')}', waiting for 'running'...")

            print(f"  [Guest Agent on {current_node}] Executing: {' '.join(command_list)}")
            result = proxmox.nodes(current_node).qemu(vmid).agent.exec.post(command=command_list)
            pid = result.get("pid")
            break
        except Exception as e:
            if attempt == retries:
                raise
            print(f"  [!] Guest agent not ready (attempt {attempt}/{retries}): {e}. Retrying in {retry_delay}s...")
            time.sleep(retry_delay)

    start_time = time.time()
    while True:
        if time.time() - start_time > timeout:
            raise TimeoutError(f"Guest command timed out after {timeout} seconds.")
            
        try:
            current_node = get_vm_node(vmid)
            
            # Check if the VM crashed or was turned off externally
            vm_status = proxmox.nodes(current_node).qemu(vmid).status.current.get()
            if vm_status.get("status") != "running":
                raise RuntimeError(f"VM {vmid} turned off unexpectedly during execution.")
                
            status = proxmox.nodes(current_node).qemu(vmid).agent.get("exec-status", pid=pid)
        except Exception as e:
            # If we explicitly raised the RuntimeError above, bubble it up to crash cleanly
            if isinstance(e, RuntimeError) and "turned off unexpectedly" in str(e):
                raise
            print(f"  [!] Transient error polling PID {pid}: {e}. Retrying...")
            time.sleep(retry_delay)
            continue

        if status.get("exited") == 1:
            exit_code = status.get("exitcode")
            if exit_code != 0:
                out = status.get("out-data", "")
                err = status.get("err-data", "")
                raise RuntimeError(f"Command failed (Code {exit_code}). Out: {out} Err: {err}")
            return status
        time.sleep(5)

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/get_templates", methods=["GET"])
def get_templates():
    try:
        vms = proxmox.cluster.resources.get(type="vm")
        templates = [
            {"vmid": vm["vmid"], "name": vm.get("name", f"VM {vm['vmid']}")}
            for vm in sorted(vms, key=lambda x: str(x.get("name", "")).lower())
            if vm.get("template") == 1
        ]
        return jsonify({"templates": templates})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/get_pools", methods=["GET"])
def get_pools():
    try:
        pools = proxmox.pools.get()
        pool_list = [
            {"poolid": p["poolid"]}
            for p in sorted(pools, key=lambda x: str(x.get("poolid", "")).lower())
        ]
        return jsonify({"pools": pool_list})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/get_files", methods=["POST"])
def get_files():
    data = request.get_json() or {}
    storage_type = data.get("storage_type", "ftp")
    vm_name = data.get("vm_name", "").strip()
    ftp_user = data.get("ftp_user", "")
    ftp_pass = data.get("ftp_pass", "")

    if storage_type == "ftp":
        try:
            ftp = ftplib.FTP(FTP_SERVER_IP, timeout=10)
            ftp.login(user=ftp_user, passwd=ftp_pass)
            all_files = ftp.nlst()
            ftp.quit()

            file_list = [f for f in all_files if f.lower().endswith(".dat")]
            return jsonify({"files": file_list})
        except Exception as e:
            return jsonify({"error": f"FTP Connection failed: {str(e)}"}), 400
    else:
        try:
            # Passes original un-sanitized vm_name with spaces for SMB lookup
            cmd = [
                "smbclient", COLD_STORAGE_SHARE,
                "-U", COLD_STORAGE_CREDS,
                "-m", "SMB3",
                "-c", f'cd "{vm_name}"; ls'
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)

            if proc.returncode != 0 and "NT_STATUS_NO_SUCH_FILE" in proc.stderr:
                return jsonify({"error": f"Folder '{vm_name}' not found on Cold Storage."}), 404

            files = []
            for line in proc.stdout.splitlines():
                clean_line = line.strip()
                match = re.search(r'^(.*\.dat)\s+', clean_line, re.IGNORECASE)
                if match:
                    files.append(match.group(1).strip())

            if not files:
                return jsonify({"error": f"No .dat backup files found in folder '{vm_name}'"}), 404

            return jsonify({"files": files})
        except Exception as e:
            return jsonify({"error": f"Cold Storage browsing failed: {str(e)}"}), 500

@app.route("/", methods=["POST"])
def deploy_from_form():
    try:
        vm_name = request.form.get("vm_name", "").strip()
        requester_email = request.form.get("requester_email")
        storage_type = request.form.get("storage_type")
        ftp_user = request.form.get("ftp_user", "").strip()
        ftp_pass = request.form.get("ftp_pass")
        backup_file = request.form.get("backup_file")
        template_override = request.form.get("template_vmid", "").strip()
        pool_override = request.form.get("pool_id", "").strip()
        ticket_id = request.form.get("ticket_id", "").strip()
        skip_freshdesk = request.form.get("skip_freshdesk") == "1"

        if not ticket_id and ftp_user:
            candidate_id = ftp_user.split("_")[0].strip()
            if candidate_id.isdigit():
                ticket_id = candidate_id

        if template_override and template_override.lower() not in ("none", "null", "undefined"):
            try:
                template_vmid = int(template_override)
            except ValueError:
                return f"Invalid Template ID selected: '{template_override}'", 400
            
            try:
                new_ip = extract_target_ip(vm_name)
            except ValueError as e:
                return f"VM Name Error: {str(e)}", 400
        else:
            try:
                template_vmid, new_ip = parse_vm_name(vm_name)
            except ValueError as e:
                return f"VM Name Error: {str(e)}", 400

        new_vmid = get_next_vmid()
        safe_vm_name = sanitize_proxmox_name(vm_name)
        template_node = get_vm_node(template_vmid)

        print(f"1. Cloning template {template_vmid} (on {template_node}) to VM {new_vmid} ({safe_vm_name})...")
        clone_params = {
            "newid": new_vmid,
            "name": safe_vm_name,
            "full": 1,
            "storage": STORAGE_POOL,
        }
        if pool_override and pool_override.lower() not in ("none", "null", "undefined", ""):
            clone_params["pool"] = pool_override

        task_upid = proxmox.nodes(template_node).qemu(template_vmid).clone.post(**clone_params)

        print("2. Waiting for disk cloning to finish...")
        wait_for_task(template_node, task_upid)
        print("   Clone complete! VM is unlocked.")

        print(f"3. Injecting Cloud-Init Network Config ({new_ip}/{NETMASK_CIDR})...")
        current_node = get_vm_node(new_vmid)
        proxmox.nodes(current_node).qemu(new_vmid).config.post(
            ipconfig0=f"ip={new_ip}/{NETMASK_CIDR},gw={GATEWAY}",
            nameserver=NAMESERVER,
            ciuser="sunbird",
            cipassword="PlumP0wderR0X",
        )

        print(f"4. Starting VM {new_vmid} (Deferred HA Registration)...")
        start_upid = proxmox.nodes(current_node).qemu(new_vmid).status.start.post()
        wait_for_task(current_node, start_upid, timeout=120)

        print(f"5. Waiting for QEMU Guest Agent on {new_ip}...")
        agent_ready = False
        for _ in range(40):
            try:
                current_node = get_vm_node(new_vmid)
                net_info = proxmox.nodes(current_node).qemu(new_vmid).agent.get("network-get-interfaces")
                if new_ip in str(net_info):
                    agent_ready = True
                    print(f"   Success! IP {new_ip} is active on node '{current_node}'.")
                    time.sleep(10)  # Allow routes and DNS to fully settle
                    break
            except Exception:
                pass
            time.sleep(5)

        if not agent_ready:
            return f"VM {new_vmid} booted but guest agent timed out waiting for {new_ip}.", 500

        print(f"6. Pulling latest dev_restore script via Guest Agent...")
        # Incorporate the dev_restore fetch logic from the vmware script
        update_dev_restore_cmd = [
            "bash", "-c",
            "curl -u \"tools:EX2PCdfa\" -sS -f -o /home/sunbird/dev_restore \"ftp://10.34.0.41/vcenter/dev_restore\" && "
            "{ chown sunbird:sunbird /home/sunbird/dev_restore 2>/dev/null || chown root:root /home/sunbird/dev_restore; } && "
            "chmod +x /home/sunbird/dev_restore"
        ]
        run_guest_command(new_vmid, update_dev_restore_cmd)
        
        backup_dest = f"/tmp/{backup_file}"
        print(f"7. Pulling {backup_file} via Guest Agent...")
        if storage_type == "cold":
            download_cmd = [
                "smbclient", COLD_STORAGE_SHARE,
                "-U", COLD_STORAGE_CREDS,
                "-c", f'cd "{vm_name}"; get "{backup_file}" "{backup_dest}"'
            ]
        else:
            download_cmd = [
                "curl", "-s", "-u", f"{ftp_user}:{ftp_pass}",
                f"ftp://{FTP_SERVER_IP}/{backup_file}", "-o", f"{backup_dest}",
            ]
        run_guest_command(new_vmid, download_cmd)

        print("8. Executing unified dev_restore script...")
        restore_cmd = [
            "bash", "-c",
            f"echo \"=== Restore Output ===\" > /home/sunbird/restore_debug.log; "
            f"sudo -S /home/sunbird/dev_restore {backup_dest} >> /home/sunbird/restore_debug.log 2>&1"
        ]
        # We can just run it since the guest agent runs commands as root by default anyway
        run_guest_command(new_vmid, restore_cmd)

        print("9. Rebooting VM via Proxmox API...")
        final_node = get_vm_node(new_vmid)
        try:
            proxmox.nodes(final_node).qemu(new_vmid).status.reboot.post()
            time.sleep(15)
        except Exception as e:
            print(f"  [!] Warning: Proxmox API reboot command failed: {e}")

        print("10. Adding VM to HA Manager...")
        try:
            proxmox.cluster.ha.resources.post(
                sid=f"vm:{new_vmid}",
                state="started"
            )
            print(f"   Success! VM {new_vmid} is now HA-protected and load-balanced.")
        except Exception as ha_err:
            print(f"   HA Warning: Failed to add VM to HA: {ha_err}")

        print("11. Sending completion email...")
        send_completion_email(requester_email, vm_name, new_ip)

        print("12. Processing Freshdesk update...")
        if skip_freshdesk:
            freshdesk_status = "Skipped (Opted out via checkbox)"
            print("  [-] Freshdesk update skipped via UI checkbox.")
        elif ticket_id.isdigit():
            _, freshdesk_status = add_freshdesk_note(ticket_id, safe_vm_name, new_ip, new_vmid, final_node)
        else:
            freshdesk_status = "Skipped (No numeric Ticket ID provided)"
            print(f"  [-] Ticket ID '{ticket_id}' is not numeric. Skipping Freshdesk update.")

        pool_display = pool_override if pool_override else "None (Unassigned)"

        return (
            f"<h2>Deployment Triggered Successfully!</h2>"
            f"<p><strong>Original Request Name:</strong> {vm_name}</p>"
            f"<p><strong>Proxmox VM Name:</strong> {safe_vm_name}</p>"
            f"<p><strong>VM ID:</strong> {new_vmid}</p>"
            f"<p><strong>Active Cluster Node:</strong> {final_node}</p>"
            f"<p><strong>Resource Pool:</strong> {pool_display}</p>"
            f"<p><strong>HA &amp; Load Balancing:</strong> Enabled</p>"
            f"<p><strong>VM Link:</strong> <a href='https://{new_ip}' target='_blank'>https://{new_ip}</a></p>"
            f"<p><strong>Backup Restored:</strong> {backup_file}</p>"
            f"<p><strong>Requester:</strong> {requester_email}</p>"
            f"<p><strong>Freshdesk Status:</strong> {freshdesk_status}</p>"
            f'<br><a href="/">Provision Another VM</a>'
        )

    except Exception as e:
        return f"Deployment failed: {str(e)}", 500

if __name__ == "__main__":
    # Removed debug=True for security. Use Waitress or Gunicorn for production instead of app.run.
    app.run(host="0.0.0.0", port=5000, debug=False)
