# Site Deploy — Changelog

## 4.4.0 — 2026-08-04

### Added
- **Drag and drop on the Plugins screen** — drop a plugin zip/file
  straight onto that window to install it, same as the main window.
- **Drop onto a specific website in the sidebar** to target that site
  directly, instead of whatever was already selected.
- Explicit import ("Import website from a file...") no longer dead-ends
  when nothing is recognized — it still tells you plainly, then opens
  Add Website so you can fill it in and save, rather than just stopping.

## 4.3.0 — 2026-08-04

Fixes a real drop-in-your-lap bug: a text file with no labels at all
("Host:", "Username:", ...) — just one bare value per line — was silently
treated as a file to upload instead of being read as login details.

### Added
- **Reads unlabeled, one-value-per-line files.** A personal template like
  `site name / ftp address / port / username / password`, filled in with
  real values and nothing else on each line, is now recognized without
  needing any "Host:"-style keys — the address is found by shape (a
  scheme like `ftp://`, or a bare domain/IP), a bare number becomes the
  port, and the lines right after the address become username then
  password. Refuses to guess at all on anything that looks like real
  prose (spaces, sentence punctuation) or a long document — this only
  ever fires on a short, template-shaped file.
- **"Import website from a file..."** in the sidebar — an explicit,
  unambiguous alternative to dragging: point it at any file and it's
  read as login details directly, no guessing about whether it was
  meant as something to upload. If nothing recognizable turns up, it
  says so plainly instead of quietly doing something else.
- Dropping a file that doesn't parse as login details now leaves a
  visible note in the activity list saying so, instead of silently
  falling back to treating it as an upload.

## 4.2.1 — 2026-08-04

Window title now shows the version at startup ("Site Deploy 4.2.1"),
per a new standing rule for all projects.

## 4.2.0 — 2026-08-04

### Added
- **Multiple websites in one dropped text/file.** A note listing several
  sites' FTP details — blank-line separated, or a continuous list with no
  blank lines — is now split correctly per site instead of grabbing the
  first host, username and password it finds anywhere in the text
  (which could previously mix fields from *different* sites into one
  wrong profile). Two or more recognized sites open a picker to choose
  which ones to add; each still goes through its own Add Website
  confirm-and-save step.
- **A real upload queue.** Drop more than one file at once — zips, single
  plugin/theme files, any mix — and they're updated one after another
  automatically for the currently selected website, each still gated by
  its own "here's where this goes, go ahead?" confirmation. A single
  file dropped alone still just stages it in step 2, unchanged. A
  dropped batch that mixes upload files with a connection-details file
  is sorted correctly: the credentials go to Add Website, the rest queue
  as updates.

## 4.1.0 — 2026-08-04

### Added
- **Drag a text FILE with FTP details, not just pasted text.** Dropping a
  `.txt`/`.md`/`.ini`/`.cfg`/`.conf`/`.log` file now reads its contents the
  same way dropped text already did — a saved note or a hosting panel's
  exported details gets scanned for host/username/password/port/folder
  and pre-fills the Add Website dialog. If the file doesn't read like
  connection details (a real file meant for the site), it falls back to
  being treated as something to upload, same as before.
- **"Which host is this on?"** in the Add/Edit Website dialog — a short
  list of common providers. Choosing one shows a hint on where to find
  FTP/SFTP details. Hostinger's is the exact path (verified against this
  app); every other host gets honest general guidance rather than a
  specific menu path that might be wrong or out of date. Never guesses or
  pre-fills the actual server address — only a wrong guess there would
  make things worse, not better. The choice is remembered when you edit
  the site again later.

## 4.0.0 — 2026-08-04

Credentials moved out of the app's own storage entirely, one copy of the
app enforced at a time, drag-and-drop, and faster repeat lookups.

### Added
- **Credentials now live in Windows Credential Manager**, not in
  `profiles.json` at all — not even encrypted. The file holds only a
  random ID pointing at the vault entry; Windows persists the real
  secret across restarts, and the app reads it from Windows only at the
  moment it needs to connect. Existing profiles with a password saved
  the old way migrate into the vault automatically, once, the first time
  they're loaded.
- **Only one copy runs at a time.** A system-wide named lock (fixed
  during testing to use the `Global\` kernel namespace, so it holds
  across every session and launch path — portable exe, installed exe,
  Start Menu shortcut, all share it) — launching a second copy raises
  the existing window instead of opening a duplicate. This is the actual
  fix for websites appearing "not saved": multiple copies sharing one
  file with no coordination is exactly how a stale window overwrites a
  newer save.
- **Drag and drop.** Drop a zip or a supported file onto the window —
  same effect as choosing it in step 2. Drop a block of text containing
  FTP/SFTP details (from an email, a hosting panel, a note) and it reads
  out host/username/password/port/folder and pre-fills the Add Website
  dialog — never saved without you pressing Save yourself.
- **Caching for the WordPress root and matched theme/plugin folders** —
  both barely ever change, so a cached answer is checked with one quick
  existence call instead of a full re-search every time. Self-corrects
  automatically if the site's layout actually changes. The Plugins
  screen shows its last-known list instantly and quietly re-checks in
  the background, instead of waiting on the network every time it opens.
- **The installer now actually gets run**, not just built: Program Files
  entry, Start Menu shortcut, proper uninstall registration.
- A `.bak` copy of profiles.json is kept before every save, so a bad
  write doesn't cost the previous good version.

### Changed
- Connection-type labels rewritten in plain language (e.g. "Secure FTP
  (also called FTPS)" instead of just "FTPS") after real confusion about
  what they meant.

## 3.3.0 — 2026-08-04

**A Plugins screen** — view what's installed, install new ones, delete
old ones, with a backup taken first. Activation is deliberately left
out: whether a plugin is switched on lives in WordPress's database, not
in its files, so FTP/SFTP genuinely can't see or change it. Everything
here works with the connection Site Deploy already has.

### Added
- **Plugins** in the sidebar — connects, finds your WordPress install,
  and lists every folder in `wp-content/plugins` with the name and
  version read from that plugin's own header.
- **Install a plugin** — pick a zip or a single file the same way as the
  main "Choose what to upload" step; it's placed using the same folder
  matching and backup-before-replace logic already built for updates.
- **Delete a plugin** — backs up its files first (into the same
  `Archived` folder as everything else, with the same sidecar note so it
  can be put back to the exact right place from History), then removes
  every file and folder underneath it from the server.
- A clearly worded, hard-to-miss warning: deleting an *active* plugin's
  files can break the site until it's deactivated in wp-admin or the
  backup is restored — since Site Deploy has no way to check that first.
- `remove_file` / `remove_dir` on both the FTP and SFTP backends, and a
  recursive delete that removes every file before removing any folder
  (neither protocol has a single "delete this and everything in it").

### Not included, on purpose
- **Activate / deactivate** — would need the WordPress REST API (an
  Application Password from wp-admin) or WP-CLI over SSH, neither of
  which this app currently uses. Revisit if that's wanted later.

## 3.2.0 — 2026-08-04

**Single files, not just zips.** Some sites (parikharch.com) are one PHP
file, not a packaged theme or plugin — the picker only accepted `.zip`
before, which locked those sites out entirely.

### Added
- **Choose a single file** — `.php`, `.css`, `.js`, images, fonts, and
  more — alongside zip packages. The file picker's filter now lists every
  supported type.
- **It finds where the file already lives.** For a plugin file (detected
  by a `Plugin Name:` header), it checks that plugin's folder first, then
  every plugin and theme folder for a same-named file, then the folder
  saved in the website's details. Whichever place already has that exact
  filename is where the replacement goes.
- **If it can't find the file anywhere**, it suggests a folder and lets
  you either accept it or **browse the server yourself** and pick the
  real folder — a "Choose the file/folder..." button in the confirmation.
- **Single-file backups.** Replacing one file downloads and saves just
  that file (not the whole folder), with a small sidecar file recording
  exactly which path it came from — so **putting it back never has to
  guess the location again**, even if the site layout changes later.
- The drop-folder watcher accepts the same file types now, not only zips.

### Fixed
- Restoring now targets the *exact* saved path for both files and
  folders, using the new sidecar record, rather than re-deciding the
  destination from scratch (which could drift if the site had changed).

## 3.1.0 — 2026-08-03

Three things asked for after the first real update: plainer names for the
connection types, a way to roll back, and a proper log.

### Added
- **Put back a version** in the sidebar: lists every copy saved before an
  update for that website, with the date and size, and puts the chosen one
  back. It goes through the same engine, so **the version it replaces is
  saved too** — a rollback is itself reversible.
- **History** rewritten. Every update, creation, restore, connection check
  and failure is recorded with what happened, the folder it went to, and
  the saved copy that goes with it. Any row that has a saved copy can be
  put back straight from the list. "Open the log file" opens the folder.
- Structured log at `%APPDATA%\SiteDeploy\history.jsonl` alongside the
  readable `history.log`. Old text-only history still displays.
- **A zip with no theme/plugin header is now matched by name** against
  `wp-content/themes` and `wp-content/plugins` before anything else. If
  nothing matches and the fallback folder looks like the whole site (it
  contains `wp-content`), it **stops and warns** instead of scattering
  files across the site.

### Changed
- **Connection types now say what they mean.** "FTPS" alone was
  meaningless; the choices are now *Automatic — secure if the server
  allows it (recommended)*, *Secure FTP (also called FTPS)*, *Plain FTP —
  NOT secure*, and *Secure SSH transfer (also called SFTP)*. Profiles
  saved with the old wording are migrated on load.
- The activity line reads "Connected securely (FTPS)" or "Connected NOT
  securely (plain FTP)", and an unencrypted connection now adds a visible
  warning telling you which setting to change.
- History rows are short and scannable; the folder, detail and backup path
  moved to the row's tooltip.

## 3.0.0 — 2026-08-03

**Choose the zip; it works out the rest.** Picking a file is now the main
way to update, and the app decides where the files belong instead of you
telling it a path.

### Added
- **Step 2 asks for the file.** A normal Choose-a-file dialog. It reads
  the zip straight away and says what it is: *"wingchun-child-1.4.3.zip —
  “wingchun-child” (a WordPress theme), 34 files"*.
- **It works out where the files go.** Reads the zip (theme via the
  `Theme Name:` header in style.css, plugin via `Plugin Name:` in a
  top-level php file), finds the WordPress install on the server by
  looking for `wp-content` (including `/domains/<site>/public_html`
  layouts), then targets `wp-content/themes/<name>` or
  `wp-content/plugins/<name>`.
- **Same-or-similar names count as an update, not a new folder.**
  `wingchun-child-1.4.3`, `wingchun_child`, `WingChun-Child-2.0` and
  `wingchun-child (1)` all match the existing `wingchun-child`. Version
  numbers, `v2`, `-final`, `-copy` and `(1)` suffixes are stripped, then
  it matches exactly, by containment, or by close spelling. The reason is
  shown ("matched by same name once the version is ignored").
- **New folders are created** when nothing matches — that's the "new"
  case, and it says so before doing it.
- **The old version is saved first.** Everything currently in the target
  folder is downloaded into
  `Deploy Inbox\<site>\Archived\<folder>_before_<timestamp>.zip`
  before anything is overwritten. It's kept **on your PC, not the
  server** — a backup inside `wp-content` could be seen by WordPress and
  switched on; one on your PC can't touch the site at all. It's a zip, so
  restoring is just choosing it in step 2 and updating again.
- **One confirmation, once it knows** — it connects, works out the
  destination, then asks: *"Found where it goes: /public_html/wp-content/
  themes/wingchun-child … a copy is saved first … go ahead?"*
- `list_entries()` and `download_file()` on both backends; recursive
  remote walk for the backup.
- "Open saved copies" under More options.

### Changed
- The three steps are now: **add your website → choose your zip →
  update my site**. Connection testing moved to More options; it isn't a
  step you have to pass any more.
- The drop-folder watcher uses the same brain — dropped zips also get
  matched, backed up and placed automatically.
- After a successful update the chosen file clears, so the window is
  ready for the next one.

## 2.3.0 — 2026-08-03

The second real setup attempt got past the address problem and hit
`450 /public_html/wp-content/themes/wingchun-child: No such file or
directory` — the login worked, the folder didn't exist. Guessing paths by
hand is a bad way to fix that, so the app now shows you the server.

### Added
- **Test now explores the server when a folder is missing.** It walks up
  the path until it finds something that exists, lists what's inside, and
  opens a dialog: *"Signed in fine — the folder just isn't there"*, with
  the deepest existing folder and its contents.
- **Pick the folder from that list**, or double-click to look inside a
  folder and go deeper. Choosing one saves it as the folder to update and
  offers to re-test straight away.
- The dialog also says when *keeping* the path is right: a child theme
  folder that doesn't exist yet gets created on the first upload.
- `dir_exists()` on both the FTP and SFTP backends — a real existence
  check, instead of treating an empty listing as "missing".
- Plain-English message for 450 / "no such file" errors.

### Changed
- A connection test that reaches the server now counts as **step 2 done**
  even if the folder needs choosing — the login is what that step proves.
- FTP listings return bare names rather than full paths, so the folder
  list reads cleanly.

## 2.2.1 — 2026-08-03

Fixes for the first real-world setup attempt, which failed with
`[Errno 11001] getaddrinfo failed` because the server address had been
entered as `ftp://145.79.213.73` and the website name as
`https://wingchunclub.in`.

### Added
- **Addresses are tidied automatically.** `ftp://host/`, `https://site`,
  `user@host:22`, trailing paths and slashes are all reduced to the bare
  host (and the port is picked up if one was given). Applied when saving
  a website, and to already-saved websites when they load — with a note
  telling you what it changed.
- **Errors now say what to do.** `getaddrinfo failed` becomes "Couldn't
  find the server … it should be just the address, with no ftp:// in
  front". Same for refused connections, timeouts, rejected logins (530),
  and permission problems (550). Raw output still goes to Technical
  details.
- A hint under the Server address field: *"Just the address — no ftp://
  or https:// in front."*
- Website names are cleaned the same way, and an existing site's **drop
  folder is renamed to match** (`https---wingchunclub.in` →
  `wingchunclub.in`), carrying any zips already in it across.

## 2.2.0 — 2026-08-03

Simpler and lighter, after "make this simpler to understand, lighter
window, and instructions as to what to do". Workflow still unchanged;
both smoke tests still pass.

### Added
- **"What to do" steps** are now the main thing on screen — three
  numbered instructions in plain English (add your website → check it can
  connect → put your zip in the folder). The step you're on is
  highlighted with the only accent button on screen; finished steps get a
  green tick. The app works out which step you're on.
- The folder to drop zips into is spelled out in a box under the steps:
  *"Put the zip for <site> in this folder: …"*
- Profiles remember whether they've ever connected successfully
  (`tested_ok`), which drives the steps and the status.
- **Appearance** setting: Light (default), Dark, or Match Windows.

### Changed
- **Light by default**, even on a dark Windows — a lighter window reads
  as calmer for a background utility. Dark is still one setting away.
- Lighter, cooler light palette (`#F7F9FC` window, softer borders).
- Fewer controls: the three dashboard tiles are now **one plain sentence**
  ("Watching 1 website folder(s) · last update just now"), and "Remove
  site" moved into the right-click menu.
- Plainer labels: "Activity" → "What's happened", "History" → "Past
  updates", "New site" → "Add a website", "Edit site" → "Change details".
- The status pill says what it means: Not set up yet / Not tested yet /
  Ready / Updating your site / Needs a look.
- A successful connection test now explains what to do next instead of
  just saying "succeeded".

### Fixed
- Step rows and the folder box could clip their text when the window was
  short; they now keep their height and the activity list gives up space
  instead.

## 2.1.0 — 2026-08-03

Visual overhaul from the design feedback. **The workflow is untouched** —
build zip → drop zip → done. Both automated tests still pass unchanged.

### Added
- New `theme.py`: Fluent-flavoured theme that follows Windows — picks up
  the **system accent colour**, switches with the **OS light/dark**
  setting, dark title bar and rounded window corners via DWM.
- Flat single-stroke **line icons drawn in code** (folder, globe,
  settings, history, check, warning, upload, …) — no image files to ship.
- **Status dashboard** as the main information area: one always-visible
  status pill (Watching / Deploying / Paused / Attention needed) plus
  tiles for websites watched, last deploy ("just now", "3 hr ago"), and
  queue length.
- **Activity timeline** with icons per step (connect → upload → live)
  instead of a wall of timestamped log lines. Raw output moved behind a
  "Technical details" disclosure.
- **History** and **Settings** added to the tray menu.
- Right-click a website for Edit / Open drop folder / Remove;
  double-click to edit.

### Changed
- Group-box borders replaced with **cards, whitespace and generous
  padding**; rounded controls throughout.
- Sidebar: rounded accent-highlighted selection, hover states, and text
  buttons (New site, Edit site, Remove site, History, Settings).
- **One primary action** — "Deploy now" is the only filled accent button;
  everything else is outlined or quiet.
- Manual deploy demoted to a collapsed **"Advanced — deploy a zip
  manually"** section, since automatic is now the main path.
- Smooth unsegmented progress bar; **indeterminate** while connecting and
  extracting, switching to determinate once files start moving.
- Muted status colours (blue idle / amber busy / red error) instead of
  saturated ones; explanatory paragraphs cut down to short labels.
- Error state is now hidden rather than greyed out in the tray menu when
  there's nothing to clear.

### Fixed
- Opening a disclosure could squeeze the panels into each other; the
  window now grows to fit (never past the screen).
- Checkbox ticks were invisible when checked.
- History rows showed the current time instead of the record's time.

## 2.0.0 — 2026-08-02

Hands-off mode. Building a website update now ends at "drop the zip in a
folder" — everything after that happens on its own.

### Added
- **One drop folder per website.** `Desktop\claude code\Deploy Inbox\<site
  name>\` is created automatically when a website profile is added (and
  renamed with the profile). A zip dropped in a site's folder deploys to
  that site — no filename matching, no ambiguity. The folder location is
  changeable in Settings.
- **System tray app.** Launching the exe goes straight to the tray (the
  window still opens on first run, when there's nothing configured yet).
  Tray menu: Open Site Deploy / Open Deploy Inbox folder / Pause watching /
  Clear error state / Quit. Closing the window hides to the tray instead
  of quitting.
- **Automatic deploy on drop.** Watches every site folder (watchdog for
  instant reaction, plus a 3-second sweep as a backstop). A zip is only
  deployed once its size has stopped changing for ~2 seconds, so a
  half-copied file is never uploaded. No confirmation dialog.
- **Deployed archive.** After a successful auto-deploy the zip moves to
  `<site folder>\Deployed\` with a timestamp prefix, so the inbox stays
  empty at a glance and nothing is ever deployed twice.
- **Notifications.** Success = a toast that auto-dismisses. Failure = a
  60-second toast *plus* a red tray icon and tooltip that stay until
  acknowledged (tray menu "Clear error state", or opening the window) —
  a failed deploy to a live site is the one case where silence is bad.
  A failed zip stays in the inbox and is not retried until it changes.
- **Deploy history** at `%APPDATA%\SiteDeploy\history.log` — timestamp,
  site, filename, success/failure, detail. Viewable via the Deploy
  History button.
- **Settings dialog**: watching on/off, Deploy Inbox location, and
  "Start with Windows" (default **off**; writes the HKCU Run key).
- Per-profile "auto-deploy" toggle, so a site can be kept manual-only.
- Timestamps in the activity log.

### Changed
- The manual flow (profiles, Browse + Deploy, Test Connection) is
  unchanged and still there — tray mode is additive.
- Editing a profile no longer resets a custom port to 21/22 unless the
  port was still on a protocol default.

### Not in this release
- **Remote backup before overwrite** — deliberately deferred. The history
  log is the audit trail for now; keep the previous zip as the rollback
  path. This is the next thing worth adding.

## 1.0.0 — 2026-08-02

- Website profiles (name, host, port, username, password, protocol,
  remote folder), password encrypted with Windows DPAPI.
- Test Connection.
- Manual deploy: browse for a zip, confirm, upload over FTP/FTPS/SFTP,
  unwrapping a single top-level folder as WordPress theme zips have.
- Auto protocol detection: FTPS first, plain FTP fallback, stated in the log.
