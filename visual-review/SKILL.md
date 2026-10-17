---
name: visual-review
description: >-
  Use when the user should look at local screenshots, renders or short videos
  and give feedback: per-item notes and drawn markup in a private browser page,
  read back as review.json and marked PNG files. Triggers on: 'visual review',
  'review these screenshots', 'let me mark up the images', 'get my feedback on
  these captures'. NOT for: automated image analysis without the user.
---

# Visual review

`review.py` shows local images and videos to the user in a private page in their browser. The
reviewer moves through the items, writes notes and draws markup on the images. You wait for the
review to end, then read `review.json` and the marked PNG files.

Everything stays on this machine. The server listens only on `127.0.0.1`. Do not upload the
captures to a service.

## Requirements

- Python 3.9 or later. `review.py` uses only the standard library.
- A current Chromium-based browser, Firefox or Safari.
- Linux, macOS or Windows.

There is nothing to install.

## Prepare the variables

1. Find the folder of this skill. It holds `SKILL.md`, `review.py` and `index.html`.
2. Put the items to review in one folder.
3. Set these variables once. Use the same `RESULTS` for every command of one review.

```bash
SKILL_DIR="/absolute/path/to/visual-review"
IMAGES="/absolute/path/to/screenshots"
RESULTS="/absolute/path/to/review-results"
QUESTION="Which capture should ship, and what must change?"
IDLE_SECONDS="1800"
CLOSE_SECONDS="30"
```

- `IMAGES` holds PNG, JPEG or WebP images and MP4, WebM or OGV videos. Only regular files
  directly in the folder count. `open` skips symbolic links, so copy the files in. It also skips
  hidden files, whose names start with a dot, such as the `._` files that macOS writes on some
  drives. The page shows the items sorted by file name, ignoring case.
- Give each file a unique name stem. `capture.png` and `capture.jpg` would share one marked PNG
  name, so `open` refuses them.
- `RESULTS` keeps the verdict. Use a dedicated folder outside `IMAGES`. Each command creates the
  folder if it is missing. On Linux and macOS, each command also sets it to mode 0700.
- `QUESTION` appears above the items.
- `IDLE_SECONDS` and `CLOSE_SECONDS` set the two timeouts. The values above are the defaults.

In PowerShell, set a variable as `$IMAGES = "C:\path\to\screenshots"`, and end a continued line
with a backtick instead of a backslash.

## Open

Use `python3`. On Windows, if `python3` does not work, use `python` or `py -3`.

```bash
python3 "$SKILL_DIR/review.py" open \
  --images "$IMAGES" \
  --results "$RESULTS" \
  --question "$QUESTION" \
  --idle-timeout "$IDLE_SECONDS" \
  --close-timeout "$CLOSE_SECONDS"
```

| Flag | Meaning |
| --- | --- |
| `--images DIR` | Required. Read the items from this folder. `review.py` never changes them. |
| `--results DIR` | Required. Store `review.json` and the marked PNG files in this folder. |
| `--question TEXT` | Required. Show this question above the items. |
| `--idle-timeout SECONDS` | End the review with `timeout` after this many seconds without an edit. Default: 1800. |
| `--close-timeout SECONDS` | End the review with `closed` after this many seconds without a heartbeat from the visible page. Default: 30. |
| `--no-browser` | Start the review and print its URL, but do not open a browser. |

Both timeouts must be positive numbers.

The command starts the review server in the background, opens the page in the browser, and
returns. On success it exits 0 and prints one JSON line:

```json
{"status": "open", "url": "http://127.0.0.1:49152/TOKEN/", "pid": 12345, "results": "/absolute/path/to/review-results", "session_id": "SESSION_ID", "browser": true, "reused": false}
```

- `url` is the page address. Its token gives full access to the review. Do not post the URL or
  open it on another machine. Unless you pass `--no-browser`, other users of this machine can read
  the URL in the process list.
- `pid` is the process ID of the server.
- `browser` is true when `open` started a browser for the URL. When it is false, give the URL to
  the user and ask them to open it in a browser on this machine.
- `reused` is true when a review already runs for `RESULTS`. `open` then prints the URL of that
  review and opens no new tab, because the page is probably open already. It still checks that
  `--images` is an existing folder with `RESULTS` outside it, and that the timeouts are positive.
  It does not apply the new `--images`, `--question` or timeout values to the running review.

On failure, `open` exits 2 and prints `{"error": "MESSAGE"}`. A failed start leaves no server
running. Do not try to start the review in another way. The message names the problem. For
example, an error that ends with `use a separate results folder` means that `RESULTS` holds a
review of other or changed files.

### Browser choice

- By default, Python's `webbrowser` module opens the default browser. It also reads the standard
  `BROWSER` variable.
- To use a specific browser, set `VISUAL_REVIEW_BROWSER` to a command. `%s` in the command stands
  for the URL. Without `%s`, the URL goes at the end. For example, `chromium --app=%s` opens a
  window without browser toolbars, and `firefox --new-window %s` opens a new window.
- A text-mode browser, such as `lynx` or `w3m`, counts as no browser.

### Timeouts

- The close timeout starts when the page first loads. It counts only while the page reports itself
  visible, so a hidden tab pauses it.
- Only saved edits reset the idle timeout. Page loads, heartbeats and viewing do not. If nobody
  opens the page, the review ends with `timeout` when the idle timeout elapses.

### Sources and resuming

- Keep each source file unchanged while its review is open. A save for a changed file fails with
  `source changed since open: NAME`. The page shows the error and keeps the edits.
- For replacement captures, use new file names and a new results folder.
- `open` on a finished results folder starts a new session and keeps the saved notes and markup
  of each file name. It refuses to resume when a file with saved notes or markup has changed or is
  no longer in `IMAGES`. It also refuses when a marked PNG in `RESULTS` has no matching file in
  `IMAGES` or no entry in `review.json`.
- `open` waits for a review that is still ending. It also cleans up after a server that died.

## Wait

Run `wait` as a background command with no time limit. It returns when the review ends. Each edit
restarts the idle timeout, so a review can last much longer than the idle timeout.

```bash
python3 "$SKILL_DIR/review.py" wait --results "$RESULTS"
```

`wait` prints one status word and exits with its code:

| Output | Exit code | Meaning |
| --- | --- | --- |
| `done` | 0 | The reviewer selected Done. |
| `closed` | 10 | The reviewer closed the tab, or the visible page stopped sending heartbeats. |
| `timeout` | 11 | The idle timeout elapsed. |
| `interrupted` | 12 | `cleanup` stopped the server. On Linux and macOS, SIGINT or SIGTERM also does this. |
| `killed` | 13 | The server stopped without recording a status. |

- Codes 10 to 13 are normal ends of a review. Read the results after each of them.
- Exit 2 prints `{"error": "MESSAGE"}`, for example when `RESULTS` holds no review.
- If a time limit stops `wait` early, run it again. It is safe to repeat.

Closing the tab sends the unsaved edits. The review then ends with `closed` after a grace of
5 seconds, or after the close timeout if that is shorter. Reloading the tab keeps the review open.

If the reviewer says that they closed the tab or finished, but `wait` has not returned:

1. Run `results` and read the notes and markup. The page saves edits while the reviewer works, so
   the results already hold them.
2. Run `cleanup` to end the review. The running `wait` then prints `interrupted`.

This also covers a close message that the browser never sent. For example, the page sends no close
message when its unsaved edits are too large for one.

## Status

```bash
python3 "$SKILL_DIR/review.py" status --results "$RESULTS"
```

`status` prints the stored `review.json` (see Results schema) and exits 0. It returns at once.
Exit 2 prints `{"error": "MESSAGE"}`.

## Results

```bash
python3 "$SKILL_DIR/review.py" results --results "$RESULTS"
```

`results` prints `review.json` in the same shape and exits 0. It sets `png_stale` from the marked
PNG files on disk. If the server died, `results` records `killed` and removes the runtime files of
the server. Exit 2 prints `{"error": "MESSAGE"}`.

Read all notes and every marked PNG before you act. When `png_stale` is true, read the markup in
`review.json` instead of the PNG. Done does not mean approval. The notes hold the verdict, so
report the words of the reviewer.

## Cleanup

```bash
python3 "$SKILL_DIR/review.py" cleanup --results "$RESULTS"
```

- `cleanup` stops the server of this results folder, if one runs, and removes stale runtime
  files. It is safe to repeat.
- It never deletes `review.json`, marked PNGs or source files.
- Exit 0 prints `{"status": "STATUS"}`, where `STATUS` is the stored status. Without a review,
  `cleanup` creates the folder and prints `{"status": "closed"}`.
- If the server does not stop within 10 seconds, `cleanup` exits 2. The error says that runtime
  ownership was retained.
- Use `cleanup` to end an abandoned or stuck review. It does not erase a verdict.

## Help and errors

- Each command accepts `-h` and `--help`. Help prints usage to stdout and exits 0.
- An argument error exits 2. It prints usage and an error line, such as
  `review.py open: error: MESSAGE`, to stderr. It prints nothing to stdout.
- A runtime error exits 2 and prints one JSON line, `{"error": "MESSAGE"}`, to stdout.
- `__serve` and `__open-url` are internal commands. Do not call them.

## Reviewer controls

- Previous and Next, or ← and →, move between the items. The number box shows the current item.
  Type a number in it to go to that item.
- The notes box beside the item holds the notes for that item. In a text field, the arrow keys
  edit the text.
- The viewer opens in View mode. In View, drag the image to pan. A click does not mark the image.
- Ctrl+wheel zooms about the cursor. Shift+wheel pans horizontally. Fit resets zoom and pan. Each
  image opens at Fit.
- Fullscreen fills the screen with the viewer and its tools. Esc leaves fullscreen first.
- Choose Pen, Arrow, Rectangle or Text and a color, then mark the image. Outside fullscreen, Esc
  returns to View.
- Marks stay on their image pixels. Zoom, pan and fullscreen do not move them. A new mark gets
  the size that it would have at Fit, at any zoom.
- For Text, click a position, type the label, then select Place text or press Enter. Shift+Enter
  starts a new line. Esc discards the label.
- Undo removes the last gesture or text label. Redo puts back what Undo removed, until a new mark
  or Clear. Clear removes all marks on the image.
- Esc during a drag discards the unfinished part of the mark.
- An image with transparent pixels shows over a checkerboard. When some pixels have alpha 0, a
  badge gives their share.
- Keys outside text fields: 1 to 5 select View, Pen, Arrow, Rectangle and Text. Alt+1 to Alt+4
  select a preset color, and Alt+5 opens the color picker. Space selects Fit. Ctrl+Z undoes.
  Ctrl+Y or Ctrl+Shift+Z redoes. Ctrl+E clears the marks of the image. ← and → change the item.
- Keys anywhere: Ctrl+F turns fullscreen on or off. Ctrl+Enter selects Done. In the text-label
  box, Ctrl+Enter places the label first. Esc, or a click outside a text field, leaves the field.
- On macOS, Cmd also works in place of Ctrl, and Option is Alt.
- A browser or the operating system can reserve some shortcuts. The buttons do the same actions.

Saving and finishing:

- Each edit saves automatically. Marks save within a fraction of a second, and notes save about
  1 second after typing stops. The save indicator shows pending, saved or failed saves. After a
  failed save, the page keeps the edits and tries again with the next save.
- Done waits for pending saves and marked PNG uploads, records `done` and stops the server. The
  page then shows "Review complete. You can close this tab."
- Closing the tab sends the unsaved edits. While saves or uploads are still running, the browser
  can ask whether to leave the page.

Marks and marked PNGs:

- Selecting a tool without drawing does not mark an image. An image without marks has no marked
  PNG.
- A marked PNG holds the full-resolution image with its marks drawn on it.
- Clear deletes the marked PNG of the image. Saved marks stay editable when the review resumes.
- A rejected arrow or rectangle preview disappears. It does not block Undo, Clear, navigation or
  Done.

## Videos

Pass the video file itself, so that the reviewer sees motion, timing and the loop seam. Do not pass
frames extracted from a video, unless the reviewer asks to check frame by frame.

- Each video is one item. It plays in the page with the controls of the browser, and it loops.
  Space plays or pauses it. A browser that blocks autoplay with sound plays it muted.
- Markup, zoom and pan are off for a video. Notes, Fullscreen, Esc, ← and →, and Done work as
  for images.
- The decoders of the browser play the video, so the formats that play depend on the browser.
  H.264 MP4 and WebM play in most current browsers. OGV plays only in Firefox.
- When the browser cannot play a file, the item shows "This browser cannot play this file. Try an
  H.264 MP4 or a WebM file. Notes can still be saved."
- `open` checks the first bytes of each video. A file whose bytes do not match its extension stops
  `open` with an error.

## Results schema

`review.json` uses schema version 1:

```json
{
  "schema_version": 1,
  "session_id": "SESSION_ID",
  "question": "Which capture should ship?",
  "status": "done",
  "done": true,
  "created_at": "2026-10-10T03:00:00.000Z",
  "updated_at": "2026-10-10T03:02:00.000Z",
  "finished_at": "2026-10-10T03:02:00.000Z",
  "images": [
    {
      "name": "capture.png",
      "kind": "image",
      "source_sha256": "LOWERCASE_HEX_DIGEST",
      "width": 1920,
      "height": 1080,
      "notes": "Keep this lighting. Move the label.",
      "updated_at": "2026-10-10T03:01:59.000Z",
      "markup": [
        {"type": "rect", "color": "#ff5c54", "width": 6.0, "x": 120.0, "y": 80.0, "w": 400.0, "h": 220.0}
      ],
      "marked": "capture.marked.png",
      "markup_revision": 3,
      "png_revision": 3,
      "png_stale": false
    }
  ]
}
```

- `status` is `open`, `done`, `closed`, `timeout`, `interrupted` or `killed`. `done` is true only
  after Done. `finished_at` is null while the review is open.
- Timestamps are UTC ISO 8601 strings with milliseconds.
- `images` lists every item in page order. `kind` is `image` or `video`.
- `width` and `height` give the size in the file header. They are null for a video. The browser
  applies the EXIF orientation of a JPEG. For a JPEG with a 90 or 270 degree EXIF rotation, the
  markup and the marked PNG therefore use the swapped size.
- `source_sha256` is the lowercase hex SHA-256 digest of the whole source file.
- `notes` holds the text of the reviewer. `updated_at` is the last time that the item changed.
- `markup` lists the marks. It stays empty for a video.
- `marked` names `<stem>.marked.png` in `RESULTS` once the page has uploaded a marked PNG for the
  image. It is null when the image has no markup, or when the page has not uploaded a marked PNG
  yet. With markup and a null `marked`, `png_stale` is true, and the markup in `review.json` is the
  record.
- `markup_revision` increases with each saved markup change, also across resumed sessions.
- `png_revision` is the markup revision that the marked PNG shows, or null without a PNG. The PNG
  also stores this number in a `visual-review-markup-revision` text chunk.
- `png_stale` is true when the marked PNG is missing or shows older markup. Then the markup in
  `review.json` is the authoritative record. An item without markup has `png_stale` false.

Each mark has a `type` and a `color` (`#rrggbb`). Coordinates and sizes are pixels of the
full-resolution image as the browser shows it.

| `type` | Other fields |
| --- | --- |
| `pen` | `width`, `points` (a list of `[x, y]` pairs), optional `group` |
| `arrow` | `width`, `x1`, `y1` (tail), `x2`, `y2` (head) |
| `rect` | `width`, `x`, `y` (top-left corner), `w`, `h` |
| `text` | `x`, `y`, `size`, `text`, `mask` (`{width, height, scale, png}`) |

- `width` is the line width.
- The page saves one pen gesture as segments of at most 512 points that share a `group` value.
  Adjacent segments share their join point. One Undo removes the whole gesture. A `group` value
  has 1 to 128 ASCII letters, digits, underscores or hyphens.
- A text mark holds its label twice: as `text`, and as a raster `mask`. `size` is the font size.
  `mask.png` is a `data:image/png;base64,` alpha mask of `mask.width` × `mask.height` pixels.
  `mask.scale` is the number of image pixels per mask pixel. The page draws the mask scaled by
  `scale`, filled with `color`, with its top-left corner at `x`, `y`.

The results folder holds `review.json`, one `<stem>.marked.png` for each image with markup whose
marked PNG was uploaded, and two empty lock files: `.visual-review.lock` and
`.visual-review-io.lock`. The lock files stay after the review. Leave them in place. While a review
runs, the folder also holds `runtime.json`, with the private URL, and a `.visual-review-SESSION_ID`
folder. The server removes both when the review ends.

## Input limits

| Item | Limit |
| --- | --- |
| Notes per item | 6,000 UTF-16 code units |
| Marks per image | 20,000 |
| Text label | 240 code points and 8 lines, after wrapping |
| Pen segment | 512 points. A longer stroke becomes chained segments. |

- The page refuses an edit over a limit and shows an error. It never shortens a note or drops a
  mark without telling the reviewer.
- The text limits apply to the wrapped label, with the line breaks that wrapping adds. A label over
  a limit stays in the text box for editing and is not placed.
- Image dimensions, file sizes and request sizes have no set limit. The memory and canvas limits of
  the browser still apply.
- The on-screen image is a copy whose longest edge is at most 2,048 pixels. Markup and the marked
  PNG use the full-resolution size.

## Behavior notes

- The page draws each marked PNG about 1 second after the last markup change and uploads it. If
  the tab closes before the last upload, `png_stale` stays true, and `marked` stays null when the
  image had no earlier upload. The markup in `review.json` is then the authoritative record.
- The marked PNG shows the image as the browser decodes it: color-managed, with 8 bits per channel.
- A browser with canvas fingerprinting protection can add noise to the pixels of a marked PNG. At
  startup, the page checks for this and shows a warning when it finds it. The check is best effort.
- An image larger than the canvas limit of the browser gets notes and markup, but no fresh marked
  PNG. The page then shows "This browser cannot render the marked PNG for this image. Your markup
  is saved."
- If the browser cannot load an image, or its decoded size does not match the file header, the
  page shows an error and no image. Markup is off for that image. Notes still work.
- The video formats that play depend on the browser. See Videos.
- Reloading the tab keeps the review. Closing the tab ends the review after the close grace.
- The session URL goes into the browser history. It works only on this machine, and only while the
  session runs.

## Common mistakes

| Mistake | Fix |
| --- | --- |
| Uploading the captures, or opening the URL on another machine | Keep the review local to this machine. |
| Stopping after `open` | Run `wait`, then `results`, and report what the reviewer wrote. |
| A command time limit that stops `wait` before the reviewer finishes | Run `wait` in the background with no time limit. |
| Treating `closed`, `timeout`, `interrupted` or `killed` as a failure | Read the results. These are normal ends of a review. |
| Reading only the marked PNG when `png_stale` is true | Use the markup in `review.json`. |
| Reporting approval that the reviewer did not give, or your own reading of the notes | Quote the notes. Done alone is not approval. |
| Reusing a results folder for new captures | Use a new results folder. |
| Changing a source file during the review | Leave the sources unchanged until the review ends. |
