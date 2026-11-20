import ftplib
import re
import os
import subprocess
from flask import Flask, jsonify, render_template, request
from proxmoxer import ProxmoxAPI
from tasks import deploy_vm_task, parse_vm_name, extract_target_ip, FTP_SERVER_IP, COLD_STORAGE_SHARE, COLD_STORAGE_CREDS

app = Flask(__name__)

# Keep a lightweight Proxmox connection here just for querying dropdowns
proxmox = ProxmoxAPI(
    os.getenv("PROXMOX_IP", "10.34.7.200"),
    user=os.getenv("TOKEN_USER", "root@pam"),
    token_name=os.getenv("TOKEN_NAME", "vmcreation"),
    token_value=os.getenv("TOKEN_SECRET", "8bd52103-e731-45c8-98f6-8f500c0ff055"),
    verify_ssl=False
)

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/get_templates", methods=["GET"])
def get_templates():
    try:
        vms = proxmox.cluster.resources.get(type="vm")
        templates = [{"vmid": vm["vmid"], "name": vm.get("name", f"VM {vm['vmid']}")} for vm in sorted(vms, key=lambda x: str(x.get("name", "")).lower()) if vm.get("template") == 1]
        return jsonify({"templates": templates})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/get_pools", methods=["GET"])
def get_pools():
    try:
        pools = proxmox.pools.get()
        return jsonify({"pools": [{"poolid": p["poolid"]} for p in sorted(pools, key=lambda x: str(x.get("poolid", "")).lower())]})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/get_files", methods=["POST"])
def get_files():
    data = request.get_json() or {}
    storage_type = data.get("storage_type", "ftp")
    vm_name = data.get("vm_name", "").strip()
    
    if storage_type == "ftp":
        try:
            ftp = ftplib.FTP(FTP_SERVER_IP, timeout=10)
            ftp.login(user=data.get("ftp_user", ""), passwd=data.get("ftp_pass", ""))
            files = [f for f in ftp.nlst() if f.lower().endswith(".dat")]
            ftp.quit()
            return jsonify({"files": files})
        except Exception as e: return jsonify({"error": str(e)}), 400
    else:
        try:
            cmd = ["smbclient", COLD_STORAGE_SHARE, "-U", COLD_STORAGE_CREDS, "-m", "SMB3", "-c", f'cd "{vm_name}"; ls']
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            if proc.returncode != 0 and "NT_STATUS_NO_SUCH_FILE" in proc.stderr: return jsonify({"error": "Folder not found"}), 404
            
            files = []
            for line in proc.stdout.splitlines():
                match = re.search(r'^(.*\.dat)\s+', line.strip(), re.IGNORECASE)
                if match: files.append(match.group(1).strip())
            return jsonify({"files": files}) if files else (jsonify({"error": "No backups found"}), 404)
        except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/", methods=["POST"])
def deploy_from_form():
    form_data = {
        "vm_name": request.form.get("vm_name", ""),
        "requester_email": request.form.get("requester_email"),
        "storage_type": request.form.get("storage_type"),
        "ftp_user": request.form.get("ftp_user", ""),
        "ftp_pass": request.form.get("ftp_pass"),
        "backup_file": request.form.get("backup_file"),
        "template_vmid": request.form.get("template_vmid", ""),
        "pool_id": request.form.get("pool_id", ""),
        "ticket_id": request.form.get("ticket_id", ""),
        "skip_freshdesk": request.form.get("skip_freshdesk")
    }
    
    # Hand the data off to Celery instantly
    # Pre-validate inputs to catch instant errors before queuing
    vm_name = request.form.get("vm_name", "").strip()
    template_override = request.form.get("template_vmid", "").strip()
    
    try:
        if template_override and template_override.lower() not in ("none", "null", "undefined"):
            extract_target_ip(vm_name)
        else:
            parse_vm_name(vm_name)
    except Exception as e:
        return f"<h3>Deployment Failed</h3><p style='color:red;'><b>Error:</b> {str(e)}</p><br><a href='javascript:history.back()'>Go Back</a>", 400

    # If validation passes, queue the task
    task = deploy_vm_task.delay(request.form.to_dict())
    task = deploy_vm_task.delay(form_data)
    
    return (
        f"<h2>Deployment Queued Successfully!</h2>"
        f"<p><strong>Job ID:</strong> {task.id}</p>"
        f"<p>The VM is currently being provisioned in the background. You will receive an email once it is fully restored and online.</p>"
        f'<br><a href="/">Provision Another VM</a>'
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
