# AWDL hold

While a Frame Workspace is running, this background service keeps the Mac's
AWDL interface down. When the Workspace stops, it brings AWDL back up, but only
if the service took it down.

AWDL carries AirDrop, Handoff, Universal Control and Sidecar. Its channel
hopping stalled the Mac's Wi-Fi for 40–90 ms every 0.46 s, which showed up on
the Frame as a hitch about once a second and as extra input delay. With AWDL
held down, Mac → router ping p90 fell from 68.1 ms to 6.7 ms (2026-10-19).
macOS turns AWDL back on whenever something asks for it, so the service checks
once a second. Those features are unavailable while a Workspace is running.

A USB-C cable between the Mac and the Frame avoids Wi-Fi entirely and makes this
unnecessary.

```sh
sudo mac/awdl-hold/install.sh     # install and start
sudo mac/awdl-hold/uninstall.sh   # remove, and turn AWDL back on
```

The service runs `/usr/local/libexec/frame-workspace-awdl-hold` as root through
`/Library/LaunchDaemons/com.frame-workspace.awdl-hold.plist`. It treats any
`frame-mac-view workspace` process as a running Workspace. Check it with
`sudo launchctl print system/com.frame-workspace.awdl-hold` and
`ifconfig awdl0` (`status: inactive` while it is holding).
`python3 -m unittest tests/test_awdl_hold.py` runs the script against stand-in
commands.
