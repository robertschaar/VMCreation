import time
from proxmoxer import ProxmoxAPI

# --- Configuration ---
PROXMOX_IP = "10.34.7.200"
PROXMOX_NODE = "pve" # Verify your node name in the Proxmox GUI (top left)
TOKEN_USER = "root@pam"
TOKEN_NAME = "vmcreation"
TOKEN_SECRET = "4729b0d0-1db6-4ba9-b8da-e41a0296c096" # Paste the secret you copied earlier

TEMPLATE_VMID = 106  # Replace with your actual template ID
NEW_VMID = 999        # A temporary ID for this test VM
NEW_IP = "10.34.9.99" # A temporary test IP
# ---------------------

print("1. Connecting to Proxmox API...")
proxmox = ProxmoxAPI(
    PROXMOX_IP, 
    user=TOKEN_USER, 
    token_name=TOKEN_NAME, 
    token_value=TOKEN_SECRET, 
    verify_ssl=False
)

print(f"2. Cloning template {TEMPLATE_VMID} to VM {NEW_VMID}...")
# The clone command returns a Task ID (UPID)
task_upid = proxmox.nodes(PROXMOX_NODE).qemu(TEMPLATE_VMID).clone.post(
    newid=NEW_VMID,
    name="api-test-vm",
    full=1,
    storage="vm-storage"
)

print("Waiting for disk cloning to finish (this may take a few minutes)...")
while True:
    task_status = proxmox.nodes(PROXMOX_NODE).tasks(task_upid).status.get()
    if task_status["status"] == "stopped":
        if task_status["exitstatus"] == "OK":
            print("Clone complete! VM is unlocked.")
            break
        else:
            raise Exception(f"Clone task failed: {task_status['exitstatus']}")
    
    print(".", end="", flush=True)
    time.sleep(5)

# The rest of your script continues here...
print(f"\n3. Injecting Cloud-Init Network Config ({NEW_IP}/20)...")

print(f"3. Injecting Cloud-Init Network Config ({NEW_IP}/20)...")

# Note: cloud-init format requires ip=IP/CIDR,gw=GATEWAY
proxmox.nodes(PROXMOX_NODE).qemu(NEW_VMID).config.post(
    ipconfig0=f"ip={NEW_IP}/20,gw=10.34.0.2",
    nameserver="1.1.1.1",
    # Optional: You can force default credentials via cloud-init if needed
     ciuser="sunbird",
     cipassword="PlumP0wderR0X"
)

print(f"4. Starting VM {NEW_VMID}...")
proxmox.nodes(PROXMOX_NODE).qemu(NEW_VMID).status.start.post()

print("5. Waiting for QEMU Guest Agent to report active IP (this verifies boot + network)...")
ip_ready = False
for i in range(40): # Wait up to ~3 minutes
    try:
        # Ask the agent inside the VM for its IP addresses
        net_info = proxmox.nodes(PROXMOX_NODE).qemu(NEW_VMID).agent.get("network-get-interfaces")
        
        # If our injected IP shows up in the agent's report, it worked
        if NEW_IP in str(net_info):
            ip_ready = True
            print(f"\nSUCCESS! Guest Agent confirmed IP {NEW_IP} is active inside the OS.")
            break
    except Exception:
        # Agent isn't running yet (VM is still booting). This is normal.
        pass
    
    print(".", end="", flush=True)
    time.sleep(5)

if not ip_ready:
    print("\nTEST FAILED: VM booted but the guest agent never reported the IP.")
