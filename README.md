# kbrowse

A keyboard-driven browser for the kitty terminal, built on a stock, modern Chromium. It is made for using a browser on a remote machine over `kitten ssh`.

kbrowse starts a normal Chromium window inside a private Xvfb display. It streams the page into kitty with the kitty graphics protocol at your terminal's full pixel resolution, and sends your keys and mouse back as real input events. It is a single Python file and needs only the standard library.

## Install (Ubuntu, including ARM64)

```sh
sudo apt install xvfb flatpak
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install flathub org.chromium.Chromium
pipx install git+https://github.com/AlexdeRuijter/kbrowse   # or: pip install --user ...
```

kbrowse is a single file, so copying `kbrowse.py` to the machine and running `python3 kbrowse.py` works just as well.

kbrowse finds the Flatpak Chromium on its own, so it needs no symlink. Without a Flatpak, it falls back to a Chromium or Chrome on your PATH (for example snap). You can choose one with `--browser`, which takes a path or a Flatpak app id such as `org.chromium.Chromium`.

## Run

From kitty on your Mac:

```sh
kitten ssh oracle
kbrowse https://www.google.com
```

Cookies and logins are kept in a profile between runs. For Flatpak Chromium the profile lives in `~/.var/app/org.chromium.Chromium/data/kbrowse-profile`, for snap Chromium the profile lives in `~/snap/chromium/common/kbrowse-profile`, and otherwise in `~/.local/share/kbrowse/profile`.

If a snap or apt Chromium crashes right away, add `--no-sandbox`. Don't add it for Flatpak, which brings its own sandbox. If something looks wrong, run with `--log /tmp/kb.log`; Chromium's own output goes to `/tmp/kb.log.chrome`.

To see which Chromium you are running, type `:version` inside kbrowse, or run `chromium --version`.

## Ad blocking

kbrowse blocks ads and trackers with [uBlock Origin Lite](https://github.com/uBlockOrigin/uBOL-home). It uses the same default filter lists as Brave's blocker: EasyList, EasyPrivacy, uBlock's own filters and Peter Lowe's list. It runs inside Chromium, so it also covers iframes and workers, and it leaves nothing extra for bot checks to see.

The first run downloads it from Google's extension server, which works over IPv6. After that, kbrowse refreshes it once a week, and keeps the old copy if the download fails. It is given access to all sites up front, so it hides ad boxes as well as blocking requests. Turn it off with `--no-adblock`. You can load other unpacked extensions with `--extension DIR`.

## Keys

kbrowse uses qutebrowser-style modes. In **normal mode**, keys drive the browser. In **insert mode**, every key goes to the page. Clicking a text field enters insert mode on its own, and Esc leaves it.

| Key | Action |
| --- | --- |
| `o` / `O` (or `t`) | Open a URL or search in this tab / a new tab |
| `go` / `gO` | Edit the current URL |
| `f` / `F` | Link hints: click / open in a new tab |
| `i` / `gi` | Insert mode / focus the first text field |
| `j` `k` `h` `l`, arrows | Scroll |
| `Ctrl-d` `Ctrl-u` | Half page down / up |
| `Space` `Ctrl-f` `Ctrl-b` | Page down / up |
| `gg` / `G` | Top / bottom |
| `H` / `L` | Back / forward |
| `r` / `R` | Reload / reload without cache |
| `/` `?` `n` `N` | Find in page |
| `J` `K` (or `gt` `gT`) | Next / previous tab |
| `d` | Close tab |
| `yy` `yt` `ys` | Copy URL / title / selected text (to your Mac clipboard, over ssh) |
| `:` | Commands: `open`, `tabopen`, `back`, `forward`, `reload`, `close`, `js`, `version`, `q`. Up/Down recall earlier entries |
| `:q`, `ZZ`, `Ctrl-q` | Quit |

The mouse works as you would expect: click, drag, wheel, and hover. Dropdowns (`<select>`) open as a kbrowse menu, where you pick with `j`/`k` and Enter or with a click. JavaScript alerts and confirms show in the status line. Pasting with Cmd-V goes into the focused field. Pop-up windows, such as sign-in windows, open as a tab and close back to the page that opened them.

## Running JavaScript (`:js`)

`:js` works like a small DevTools console:

```
:js document.title                      → shows the result in the status line
:js await fetch("/api").then(r => r.status)
:js console.log("links:", document.links.length); return 5
:js -main window.someVariable           → run in the page's own JavaScript world
:js -f readable                         → run ~/.config/kbrowse/scripts/readable.js
:js -f                                  → list your scripts
```

A single expression shows its value, and it can use `await`. Several statements run as a function body, so `return` the value you want to see. `console.log`, `console.warn` and similar calls are captured. Longer output opens in a pane, where `j`/`k` scroll, `y` copies it all, and `q` closes it.

To keep this safe:
- **Private by default.** Code runs in kbrowse's own isolated world. It shares the page's DOM but none of its JavaScript, so the page can neither see your code nor tamper with what it uses. Use `-main` only when you need the page's own variables; there, the page could notice or interfere.
- **Not visible to bot checks.** Output is captured without turning on `Runtime.enable`.
- **Not blocked by the page.** The code is not run through `eval()`, so a page's Content-Security-Policy can't block it.
- **Only your own script files.** Scripts load from `~/.config/kbrowse/scripts/` only. kbrowse refuses names with `/` or `..`, symlinks out of that folder, and files or a folder that other users can write to. A script runs with access to every site you are signed in to.
- **No escape codes.** All output is stripped of control characters before it reaches the terminal.

TypeScript isn't supported yet.

## Options

| Option | Meaning |
| --- | --- |
| `--scale N` | Device pixel ratio. The default is 2 when kitty's cells are tall (Retina), otherwise 1 |
| `--downscale F` | Send frames at a fraction of full resolution, e.g. `0.5` on a slow link |
| `--headless` | Use headless Chromium instead of Xvfb. Easier for sites to detect |
| `--browser PATH` | Which Chromium/Chrome to run |
| `--profile DIR` | Profile directory |
| `--no-adblock` | Don't load uBlock Origin Lite |
| `--extension DIR` | Also load an unpacked Chromium extension (repeatable) |
| `--restore` | Keep the tabs Chromium restores from last time |
| `--right-click` | Send right clicks to the page (native context menus are not shown) |
| `--chrome-flag=--proxy-server=...` | Pass any extra flag to Chromium |
| `--cdp pipe\|port` | How to connect to Chromium. The default tries the pipe, then a local port. Flatpak always uses the port, since the pipe can't get through the sandbox |

## Staying clear of bot checks

- Chromium runs as a normal desktop browser (windowed, in Xvfb). Its user agent and brands are those of a regular Chrome, `navigator.webdriver` is false, the page is visible and focused, and WebGL works.
- kbrowse never calls `Runtime.enable`, never injects scripts into pages, and never overrides the user agent or screen metrics over DevTools. Its own scripts (link hints, find, dropdowns) run in an isolated world that the page cannot see.
- All clicks, mouse moves, and keys arrive as trusted events, including hover movement.

## Limitations

- Native browser UI is not shown: right-click menus, autofill popups, permission prompts, and the download bar. Downloads go to `~/Downloads` on the VM.
- Each frame is a PNG of the whole page area. On a slow link, use `--downscale`.
- Needs kitty, or another terminal that supports the kitty graphics protocol and reports its size in pixels.

Tests: `python3 -m unittest test_kbrowse`.
