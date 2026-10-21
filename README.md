# DIGI GRG-4355u Scripts

This project targets the **Comtrend GRG-4355u** supplied by **DIGI, the Spanish ISP**, at the time of writing (October 2026).
[BandaAncha describes DIGI’s introduction of this model](https://bandaancha.eu/articulos/digi-acaba-definitivamente-ont-apuesta-11855) in July 2026.

The goal is to use the Comtrend as an optical terminal and let your own router manage the internet connection.
LAN5 provides the 10G handoff. Your router handles PPPoE, routing, firewall rules, and Wi-Fi.

The DIGI interface hides configuration needed for this setup. These two Python scripts expose additional LuCI menus and configure the bridge.
No SSH, package installation, or replacement firmware is required for the scripts.

**Use at your own risk. This project is provided as is, without warranty.**
**The authors accept no responsibility for router damage, loss of configuration, loss of connectivity, or other consequences.**
Use these tools only on a device you own or are authorized to administer.

## Exact router and firmware

This procedure is only for the observed **Comtrend GRG-4355u, hardware 1.1, with DIGI firmware DIG-1.3.22**.
Other models and firmware versions are unsupported and untested. Future updates can patch the privilege escalation or overwrite the configuration.

After login with your normal local account, open the **Status (Estado)** panel to find the router model and firmware version.
This check requires no additional permissions or script execution. It identifies the installed version; it does not change the firmware.

The router runs a vendor-customized version of **OpenWrt** internally, with LuCI as its web interface.
The board reports `econet,en7581`, target `airoha/grg4355u`, and kernel `5.4.55`.
This project does not turn the device into a supported upstream OpenWrt router or provide a verified root shell.

## Files and why they exist

| File | What it does | Why it is needed |
| --- | --- | --- |
| [`enable_admin.py`](enable_admin.py) | Gives the existing local account all defined RPC permission groups and verifies a fresh login. | Exposes the full LuCI configuration menus. |
| [`enable_bridge.py`](enable_bridge.py) | Bridges fiber VLAN 20 to LAN5, with a rollback timer. Also contains the shared login/API client. | Lets your own router establish PPPoE through the 10G port. |
| [`.gitignore`](.gitignore) | Excludes generated reports, recovery files, backups, and firmware dumps. | Keeps private router data out of Git. |

Both scripts use only the Python standard library. Keep both files in the same directory.
There is no test framework, CI pipeline, or installation process.

## How the privilege-escalation vulnerability works

This is an **authenticated authorization misconfiguration** in the observed vendor firmware.
It requires valid local credentials and access to the router management interface.

LuCI uses `rpcd` to authorize API requests. The `/etc/config/rpcd` configuration assigns permission groups to each login.
The local `user` account has a restricted group list, but that list includes `allow-full-uci-access`.
On the observed device, the account can consequently write the `rpcd` configuration that defines its own permissions.

That is the privilege boundary failure: a restricted account can edit the rules that restrict it.

The script performs these steps through the existing HTTPS API:

1. Log in with the credentials supplied at the terminal prompt.
2. Read the `rpcd` configuration through UCI.
3. Find the login section matching the supplied username.
4. Save its existing `read` and `write` permission lists locally.
5. Replace those lists with `["*"]` and commit only `rpcd`.
6. Create a fresh login and verify access to the WAN configuration group.

The relevant UCI section then has this shape:

```text
config login
    option username 'user'
    # Existing authentication configuration stays unchanged.
    list read '*'
    list write '*'
```

The existing `admin` login already had these wildcard lists. The script gives the selected account the same group selection.
It does not guess the admin password, overwrite passwords, disable ACL checks, or exploit a memory-corruption bug.

`*` selects all defined permission groups. It does not automatically permit every filesystem path or shell command.
That explains why more menus appear while root access and some vendor controls remain restricted.
This behavior was observed on this DIGI build. It is not a claim that standard OpenWrt grants these permissions to restricted users.

## 1. Enable the full menus

Use Python 3 in an interactive terminal on macOS or Linux.
Connect the computer to Comtrend Wi-Fi or LAN1–LAN4. Keep this management connection during the procedure.
From the directory containing both scripts, run:

```sh
python3 enable_admin.py
```

Enter the current local router username and password. The password is not displayed or saved by the script.
Then log out of the router web interface and log in again at:

**[https://192.168.1.1/cgi-bin/luci/admin](https://192.168.1.1/cgi-bin/luci/admin)**

The scripts accept this router's self-signed HTTPS certificate. Requests bypass environment proxies and reject redirects to another origin.

## 2. Immediately save a configuration backup

**Make this your first action after enabling the full menus, before bridge, firewall, theme, or package changes.**

1. In the admin interface, open **System → Backup / Flash Firmware** or the equivalent backup/restore page.
2. Download the configuration backup to your computer.
3. Store it privately and label it with the router model, firmware version, and date.

The backup can contain passwords and optical credentials. Do not upload it to this repository or a public issue.
If backup is available before the permission change, save an additional copy before running the script.

The scripts' recovery files contain only the settings they change. They are not full router backups.
A configuration backup does not contain removed package binaries and cannot repair a damaged firmware image by itself.

## 3. Configure the 10G bridge

The bridge provides this path:

```text
Fiber VLAN 20 (pon.20) ↔ br-bridge ↔ eth0.5.20 ↔ LAN5 (10G) ↔ your router WAN, VLAN 20
```

The interface is named **`bridge`** and the Linux device is **`br-bridge`**.
Each VLAN device removes the tag on ingress and adds it on egress. Your external router must use VLAN 20.

1. Request your PPPoE credentials from DIGI support.
2. Connect Comtrend LAN5 (10G) to your router's WAN port.
3. Configure that router for PPPoE with your DIGI credentials and VLAN ID **20**.
4. Run:

   ```sh
   python3 enable_bridge.py
   ```

5. Keep the computer connected to Comtrend Wi-Fi or LAN1–LAN4.
6. Connect a second device to your own router.
7. Verify that PPPoE connects and that the second device can open a website.
8. Before the displayed deadline, type `KEEP` in the script terminal.

The script removes LAN5 from `br-lan` and disables the Comtrend's local PPPoE interface and its dependent DHCPv6 interface.
It preserves their credentials, the optical configuration, Wi-Fi, the management LAN, and the separate `iphost_0` service.
The external router takes over internet routing. Comtrend Wi-Fi remains available for management at `192.168.1.1`.

Without `KEEP`, the router automatically rolls back after five minutes. The input deadline reserves 30 seconds for final API requests.
Do not reboot during that interval. The rollback mechanism does not establish safety across a reboot.

## Recovery and inspection

For inspection without configuration changes:

```sh
python3 enable_admin.py --dry-run
python3 enable_bridge.py --dry-run
```

To restore the settings saved by a previous run, use the actual path printed by that run:

```sh
python3 enable_admin.py --restore /path/to/admin-recovery-TIMESTAMP.json
python3 enable_bridge.py --restore /path/to/before-TIMESTAMP.json
```

Run only the recovery command relevant to the change you want to undo.
The bridge recovery command also uses the rollback timer and `KEEP` check.
Recovery restores the pre-run state, which can already contain a fault.



## Root password and SSH limitations

The full interface exposes a password page, but the owner reports that changing the root password fails on this build.
The screenshot shows the page, not a successful password change.
SSH with the local `user` account opens a restricted vendor CLI. It does not provide a Linux shell.

![Exposed password page](img/root-password-change.png)

## Future work: firmware extraction and full access

Contributions toward a firmware dump, firmware analysis, and a complete router unlock are welcome.
The project did not recover admin credentials or obtain verified root access.
The scripts expand the existing account’s RPC permissions, but vendor restrictions remain.

One possible research direction is offline password recovery from hashes extracted from an authorized device or its firmware dump.
On OpenWrt, password hashes normally reside in `/etc/shadow`; `/etc/passwd` usually contains account records and a placeholder.
See the [OpenWrt password-storage discussion](https://github.com/openwrt/openwrt/issues/8910) and the [passwd manual](https://man7.org/linux/man-pages/man5/passwd.5%40%40shadow-utils.html).
The storage format on this vendor build still needs verification.
An offline brute-force attempt depends on access to the hashes and password strength; success is not guaranteed.
This repository contains no firmware extraction or password-cracking tool.
Firmware dumps and password hashes must remain private because they can expose device or subscriber credentials.

## Optional: remove the TR-069 packages

The owner could not disable the TR-069 toggle with `user`, including after the full menus appeared.
The reason for that UI restriction remains unverified.

The owner supplied this package-removal procedure for the same build. It can interrupt ISP provisioning, remote support, and managed updates.
Before removal, record the exact package versions and establish how to restore the packages or original firmware.
A configuration restore alone does not reinstall packages.

In the admin interface, open **System → Packages / Software** and select the installed-package list.
Remove these exact packages, checking each result before proceeding:

- `tr069_xmpp`
- `tr069_stun`
- `easycwmp`
- `tr069_upnp`

If removal requires additional packages or a forced dependency override, stop and inspect those dependencies.
Do not substitute similarly named LuCI menu packages. Removing a menu does not prove that the agent stopped.

![Package manager reports EasyCWMP removal](img/remove-tr069.png)

The screenshot reports removal of `easycwmp` and retention of the modified `/etc/config/easycwmp` file.
The retained configuration file alone does not establish that the agent is still active.
The screenshot does not establish removal of all four packages or the runtime result after reboot.

Verify that the packages are absent, the agent no longer runs, and no new ACS sessions occur.
After removal completes, reboot at a suitable time and repeat the checks, including internet and optical-link checks.
Removing these components does not grant root access or eliminate every ISP management mechanism, including optical OMCI.

## Optional: disable Wi-Fi

LAN1–LAN4 and Comtrend Wi-Fi remain available for management, without internet access through the Comtrend.
After you verify management access through a wired port, disable Comtrend Wi-Fi to reduce interference with your own router.


## Optional: select the Bootstrap theme

Open **System → System → Language and Style**. Select **Bootstrap**, then save the theme change.

![Bootstrap theme selection](img/theme-settings.png)

## References and sharing

- [OpenWrt RPC session permissions](https://github.com/openwrt/rpcd/blob/master/session.c)
- [OpenWrt UCI RPC implementation](https://github.com/openwrt/rpcd/blob/master/uci.c)
- [Broadband Forum TR-069 specification](https://www.broadband-forum.org/pdfs/tr-069-1-6-1.pdf)

Keep backups, recovery files, serial numbers, optical credentials, and subscriber details private.
Review screenshots before sharing. `.gitignore` excludes generated data but cannot sanitize screenshots or text you paste into an issue.
This project is independent of DIGI, Comtrend, and OpenWrt. No vendor firmware is distributed here.
