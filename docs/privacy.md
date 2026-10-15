# Privacy and analytics

Frame Control sends anonymous analytics to [PostHog](https://posthog.com)
(US cloud) so the maintainer can see how many people use it, which features
matter and where installs fail. You choose how much in **Privacy & updates**,
the last panel on the page. `ui/frame_telemetry.py` is the whole
implementation.

## The three levels

| Level | Default | What it sends |
|---|---|---|
| Anonymous usage statistics | On, after a notice on first run | The events in the table below |
| Share compatibility results | Off | Your Android compatibility reports and tests |
| Send error details | Off | Scrubbed error messages and tracebacks |

Nothing is sent until the first-run notice has been shown. The notice's
**Share more to help fix problems** button turns on the second and third
levels together. Either can be turned off later. Turning a level off
deletes that level's events that haven't been sent yet.

**Show what's been sent** in the panel lists the last 50 events that left your
computer, exactly as they were sent.

## Anonymous

- Events carry a random id, made when Frame Control first runs and kept in
  its data folder (`telemetry/settings.json`). It isn't derived from your
  computer, account or network. To get a new one, delete that file.
- Events are sent without person profiles (`$process_person_profile: false`)
  and without location lookup (`$geoip_disable: true`). Each carries a
  placeholder address (`$ip: 0.0.0.0`), so PostHog stores that instead of
  yours.
- Every event includes the app version, OS name (macOS, Windows or Linux),
  CPU architecture and Python version.
- `app_installed`, `app_updated` and `app_opened` carry `country`: a
  two-letter code such as `DE`, worked out on your computer from its time zone
  setting (`Europe/Berlin` becomes `DE`), so we can see roughly where Frame
  Control is used without knowing your address. The time zone itself isn't
  sent, and a setting that isn't a country's zone (such as UTC) sends nothing.
  Copies that showed the first-run notice before it mentioned this (before
  0.4.6) show it again, and don't send a country until it has been seen.
- `frame_connected` carries `headset_id`: the first 16 hex digits of a
  SHA-256 hash of the headset's `/etc/machine-id`, worked out on the headset,
  so the machine-id itself never leaves it. It is the same for one headset on
  every computer, which lets us count headsets rather than installs; it can't
  be turned back into the machine-id or a serial number. `headsets_saved` is how
  many headsets this copy of Frame Control has set up.

## Usage events

| Event | When | Properties besides the common ones |
|---|---|---|
| `app_installed` | First run | `country` |
| `app_updated` | First run of a new version | `from_version`, `country` |
| `app_opened` | At most once a day | `country` |
| `frame_connected` | The first time a SteamOS build is seen on each headset | `steamos_build`, `steamos_version`, `headset_id`, `headsets_saved` |
| `tab_viewed` | The first click on each tab in a session | `tab` |
| `install_finished` | Any install finishes, working or not | `kind` (apk, flatpak, steam, title, web), `ok`, `seconds`, `error_category` (only when `ok` is false), `installer_code`, `xr_layer_missing`, and see below |
| `update_offered`, `update_started`, `update_failed` | The update banner | `to_version`, `error_category` |

`install_finished` never includes a file name, path or error message. An
error becomes one category from this fixed list, plus Android's own
`INSTALL_FAILED_…` code (`installer_code`) when there is one:
`android_installer`, `apk_needs_newer_android`, `apk_wrong_abi`,
`layer_missing`, `apk_repack_failed`, `tool_missing`, `apk_unreadable`,
`cant_run_on_frame`, `steam_shortcut`, `steam_not_running`,
`steam_not_responding`, `steam_js_error`, `artwork_failed`, `copy_timeout`,
`frame_not_set_up`, `frame_auth`, `frame_unreachable`, `frame_disk_full`,
`copy_failed`, `download_failed`, `flatpak`, `cancelled`, `lepton`,
`internal_error` (a mistake in Frame Control's own code) or `other`. A VR
APK that installed without Frame Control's OpenXR compatibility layer,
because this copy of the app is missing it, carries `xr_layer_missing: true`;
otherwise that field is left out. It names what was installed only when
that's already public:

- F-Droid catalogue apps: `package`. Never the version, since a local build can reuse a
  catalogue app's package name
- Flathub apps: `flatpak_id`
- Steam games: `steam_appid`
- A sideloaded title: only its runtime (Proton or Linux)

Any other APK is sent as `catalog: false`, with no name.

## Compatibility results (opt-in)

Each report becomes a `compat_report` event with the fields the Report dialog
shows: package, version, result or rating, your notes, how it was run, and the
SteamOS and Lepton builds. Before sending:

- the notes, app name and version are scrubbed like error messages (see
  below)
- the APK's source is kept only if it's `F-Droid`, `meta-store` (a test
  from [My Meta library](meta-library.md)) or the public host name of a
  download link (`https://example.com/…`). File names, user names,
  passwords, ports, paths, IP addresses and local host names are dropped

When you turn this on, reports you made earlier on this computer are shared
too.

My Meta library (Experimental) keeps your Meta access token in
`meta-account.json` in this data folder, owner-only, and sends it only to
Meta's API and download hosts. It is never in analytics, reports, error
details or logs; its test reports carry the package, version, version code,
title, result and the reason, nothing from your account. See
[meta-library.md](meta-library.md#your-token-and-privacy).

The maintainer's `python3 ui/frame_compat_db.py sync` copies these events
into the compatibility database, marked `via=community…`. It takes at most
30 per reporter per day.

## Error details (opt-in)

`$exception` events carry an error message, the Frame Control file, line and
function it came from, and the request that failed (for example
`POST /api/android install`). Before anything is sent, the message is
scrubbed:

- your home folder becomes `~`, and any user name becomes `<user>`
- IP and MAC addresses, email addresses, `.local`, `.lan` and Tailscale host
  names, Steam ids, SSH and PEM keys, API tokens and long hex strings are
  replaced
- URLs are cut down to their scheme and a public host name, or `<url>`. User
  names, passwords, ports, paths and queries are dropped
- `token=`, `key=`, `password=` and similar values are replaced

The same error is sent at most once every 10 minutes. When ssh reports that
it couldn't reach the Frame (asleep, away, or not set up yet), or that ssh and
the Frame refused each other's keys (a changed host key, or this computer's
key not accepted), that is sent at most once per kind each time Frame Control
runs.

## Report a problem

**Report a problem** is the warning-sign button in the header, also in the
Privacy panel and under **Help → Report a Problem…**. It sends the report
privately to Frame Control's PostHog project as a `problem_report` event, the
same way as the analytics above, so only the maintainer can read it and
nothing is published. It works whatever the analytics settings are, because
the person sends it deliberately. The report has the kind, title and text you
wrote, a short reference shown after sending, and the diagnostics below. Your
email address goes with it only if you tick **The maintainer may contact me
with follow-up questions** (the report then carries `contact_followup: true`);
it's filled in from **Contact email** below when you've agreed there. It has its own random id, so it isn't linked to
your analytics events. With that box ticked, the address also becomes your
**Contact email** below with follow-up questions ticked, so you remove it there
like any other. If it's a different address from the one saved there, it
replaces it, and update notices stop until you turn them on again (they were
agreed for the old address); the form says so before you send. The report then also
carries this copy's contact id and change number (`contact_id`, `contact_rev`,
see below), so removing or changing the address later takes back the
follow-up permission given with the report too.

With **Include diagnostics** ticked (the default), the report adds:

- the app version and whether it's a built app
- the OS, its release and CPU, and the Python version
- the Frame's SteamOS build, if it has connected since the app started
- which analytics levels are on
- a short connection summary, so "the app can't find the headset" can be
  diagnosed. It is made of fixed words and counts only; no text from an
  error message or from ssh, and no name you chose, goes in it:
  - the connector's state (idle, connecting, connected or failed), the stage
    it failed at (network, find, ssh, identity or login), the attempt number
    and why it started (such as "Starting up" or "Trying again")
  - how many headsets are saved; whether the active one's ssh alias is the
    default `frame` or a custom one (a custom alias itself is never named);
    whether it's saved or a bare ssh alias; and its number of addresses
  - for the current error and the last failure: the stage and an error
    category (the same fixed names as error details, such as
    `frame_unreachable`, `frame_not_set_up`, `frame_auth` or `other`), and
    how long ago the last failure was. The category is decided when the
    failure happens
  - for each address tried, only its kind (`.local`, `ipv4`, `ipv6`,
    `ipv6 link-local`, `tailscale`, `hostname`, `alias` for one read from
    `~/.ssh/config`, and what a name resolved to, such as
    `.local->ipv6 link-local`) and how the try went (answered, unresolved,
    timeout, refused, unreachable, sshfailed). Never the address itself
  - when connected, the kind of address used and its round trip in ms
  - whether this computer has a network gateway, and whether Tailscale is on,
    off or not installed
  - which kind of `ssh` the app runs (Windows OpenSSH in System32, OpenSSH in
    Program Files, Git for Windows, MSYS2/Cygwin, Homebrew or /usr/local,
    Nix, the system one, or "other"), never its path; its version, rebuilt
    from the numbers in the banner `ssh -V` prints (such as
    `OpenSSH for Windows 9.5p1, LibreSSL 3.8.2`; anything that isn't a
    plain OpenSSH banner is reported as `unknown`); and the kinds of any
    other `ssh` on the PATH.
    This is looked up once in the background after the app starts, so a
    report sent straight away may say "still being checked"
  - whether `~/.ssh/config` exists, whether it has Set Up Connection's
    managed block for the active alias, how many managed blocks it has, and
    whether a hand-written `Host` line also names the alias (yes or no; the
    alias isn't named)

**Also include recent activity and the server log** is off by default,
because those lines can name files and apps. When ticked, it adds the newest
Activity lines and server log lines, without the request lines. The server
log has a line for each failed connection attempt: its stage, the message
shown on the connection pill, ssh's last line about it, and the address kinds
above. That free text is scrubbed when the line is written: the addresses, ssh alias
and display name of the headset being tried (whole, whatever their case), and
then any name ssh gives after "hostname", "host" or "to", become `<host>`; a Windows home folder's
whole name (spaces and apostrophes included) becomes `<user>`, and ssh's
whole `user@host:` field (spaces, `DOMAIN\user` and full domain names
included) becomes `<user>@<host>:`; then the scrubbing below. A host name ssh mentions
in some other wording can still get through, so check the log lines in **Show
exactly what's included** before sending. A failure that repeats on every
retry is written at most once every 5 minutes.

Everything is scrubbed like error details and limited to what fits in the
report. Environment details are kept first, then the newest lines. **Show
exactly what's included** shows the snapshot that will be sent, and later
activity isn't added to it. If PostHog can't be reached, **Copy report** puts
the whole report on the clipboard.

The maintainer reads reports on the Frame Control dashboard in PostHog, or
with `python3 ui/frame_report.py inbox [days]`, which uses the same personal
API key as `frame_compat_db.py sync`.

## Contact email (optional)

Frame Control never needs an email address. If you'd like to leave one, there
are two separate choices, both off until you tick them:

| Choice | What it's for |
|---|---|
| **Email me about Frame Control updates** | Occasional notices about new releases and updates |
| **The maintainer may contact me with follow-up questions** | Questions about problem reports you send, mostly |

You're asked once, in a bar at the top of the page, after the Frame has
connected for the first time, and never in the same visit as the first-run
privacy notice. **No thanks** hides it for good, and it isn't
shown again even if you ignore it. **Contact email** in **Privacy & updates**
is where you add, change or remove the address and either choice at any time.

**What's sent, and where.** The address and the two choices go privately to
Frame Control's PostHog project, the same place as problem reports, as a
`contact_consent` event with `email`, `updates`, `followup`, `action` (`set`
or `withdraw`) and the common properties above. Only the maintainer can read
that project, and nothing in it is published or shared. It's sent only when
you save, or when you send a problem report with follow-up questions ticked,
whatever the analytics settings are, because you chose to. With a report, the
address and choices are saved before the report is sent and stay saved if it
fails; like any change, they're sent as soon as PostHog can be reached. It
carries its own random contact id, not the analytics id, so it isn't linked
to your usage events, and a `rev` number that goes up with each change, so
the newest choice always wins. Like everything else sent, it's listed under
**Show what's been sent**. On this computer the address and choices are kept in
`contact/contact.json` in Frame Control's data folder. An address is only
kept with at least one choice ticked.

**Removing it.** **Remove my email** (or clearing the address and saving)
deletes it from this computer, including from the **Show what's been sent**
log (in earlier contact events and problem reports), and sends a `withdraw`
event with no address in it. The maintainer's list only uses the newest event from each copy, so from
then on the address isn't listed for either choice. Unticking one choice
works the same way for that choice. This also covers problem reports you sent
from this copy with follow-up questions ticked: if your newest choice since the
report (by change number, not the clock) no longer agrees to follow-up
questions at that address, the maintainer's inbox shows the permission as
withdrawn and leaves the address out. If you're offline, the change waits on
this computer and is sent when PostHog can be reached. The earlier event
stays in PostHog until its data retention removes it; to have it deleted
sooner, ask the maintainer (for example in a problem report).

Nothing sends email yet: this only records who agreed to what. The
maintainer lists the addresses with
`python3 ui/frame_report.py contacts [updates|followup]`, which uses the same
personal API key as `inbox`.

## Turning it all off

Untick the boxes, or set `DO_NOT_TRACK=1` or `FRAME_CONTROL_TELEMETRY=0` in
the environment that starts Frame Control. A copy run from a source checkout
never sends analytics unless `FRAME_CONTROL_TELEMETRY=1` is set.

These switches cover the analytics above. A problem report or a contact email
is sent only because you pressed its Send or Save button, so those still go
when you choose to send them (a contact change saved while offline is sent
by itself once PostHog can be reached); if you don't, nothing is sent.

## Update checks

The desktop app asks GitHub for the latest release shortly after starting,
then every 6 hours: the latest release's `update.json` on GitHub, or
`api.github.com/repos/saphid/frame-control/releases/latest` if that fails.
Those requests carry no id. To stop it, set
`FRAME_CONTROL_NO_UPDATE_CHECK=1`. See [releasing.md](releasing.md).
