file_path = '/opt/vmcreator/vm_orchestrator.py'
with open(file_path, 'r') as f:
    code = f.read()

old_text = 'echo "Restarting services"\nbounce\n"""'
new_text = 'echo "Restarting services"\nbounce\n\necho "Initiating final reboot..."\nnohup sudo bash -c \'sleep 5; reboot\' >/dev/null 2>&1 &\n"""'

if 'Initiating final reboot' not in code:
    with open(file_path, 'w') as f:
        f.write(code.replace(old_text, new_text))
    print('Delayed reboot successfully added.')
else:
    print('Reboot is already in the script.')
