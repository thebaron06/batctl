# Install / Enable / Start
Run as root (or using sudo):

1) Copy the two files (service, timer) to `/etc/systemd/system`
2) Reload systemd
   `sudo systemctl daemon-reload`
3) Enable  start the timer immediately
   `sudo systemctl enable --now batctl.timer`
4) Check timer status
   ```
   sudo systemctl list-timers --all | grep batctl
   sudo systemctl status batctl.timer
   sudo systemctl status batctl.service    # shows last run
   ```

# Stop / Disable

To stop or disable run (as root or using sudo):

```
sudo systemctl stop batctl.timer
sudo systemctl disable batctl.timer
```

# Where to see logs

You can inspect the logs using either:

* `journalctl`
  `sudo journalctl -t batctl -f`

* Or check `/var/log/syslog` (or other default locations of syslog if changed)
  `sudo tail -f /var/log/syslog | grep batctl`

