import os

hook = os.path.expanduser("~/.claude/hooks/send-to-telegram.sh")
with open(hook) as f:
    content = f.read()

if "HOOK_DEBUG" not in content:
    debug_line = 'exec 3>&0; echo "$(date): HOOK_DEBUG called" >> /tmp/hook_debug.log\n'
    content = content.replace(
        "#!/bin/bash\n",
        "#!/bin/bash\n" + debug_line,
        1
    )
    with open(hook, "w") as f:
        f.write(content)
    print("Debug logging added to hook")
else:
    print("Debug already present")
