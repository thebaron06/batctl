# Install / Enable / Start

Run `crontab -e` and add the following line:

```
*/5 * * * * /path/to/batctl/scripts/cron/cron-wrapper-batctl.sh
```

# Stop / Disable

Run `crontab -e` and either comment the line that was added or remove it.

# Where to see logs

You can inspect the logs using either:

* `journalctl`
  `sudo journalctl -t batctl -f`

* Or check `/var/log/syslog` (or other default locations of syslog if changed)
  `sudo tail -f /var/log/syslog | grep batctl`
