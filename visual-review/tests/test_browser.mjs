#!/usr/bin/env node
/** Browser tests: real page actions, checked against review.json and the exported pixels.
 * Run: cd visual-review/tests && npm install && npx playwright install chromium firefox webkit
 * then: node test_browser.mjs --browser chromium|firefox|webkit
 * Browsers run headless. VISUAL_REVIEW_TEST_FOCUS selects one section; the default is all.
 */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import {
  mkdtemp,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { createDeflate, createInflate, deflateSync } from "node:zlib";
import { chromium, firefox, webkit } from "playwright";

const engines = { chromium, firefox, webkit };
const { values: options } = parseArgs({
  options: { browser: { type: "string", default: "chromium" } },
});
if (!Object.hasOwn(engines, options.browser)) {
  console.error("--browser must be chromium, firefox or webkit");
  process.exit(2);
}
const tests = dirname(fileURLToPath(import.meta.url));
const skill = dirname(tests);
const script = join(skill, "review.py");
const lockFiles = new Set([".visual-review.lock", ".visual-review-io.lock"]);
const root = await mkdtemp(join(tmpdir(), "visual-review-browser-"));
const images = join(root, "images");
const results = join(root, "results");
await mkdir(images);

// PNG specification fixture, independent of review.py: a 320x240 white RGB image.
function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++)
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const kind = Buffer.from(type);
  const size = Buffer.alloc(4);
  size.writeUInt32BE(data.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([kind, data])));
  return Buffer.concat([size, kind, data, checksum]);
}
const header = Buffer.alloc(13);
header.writeUInt32BE(320, 0);
header.writeUInt32BE(240, 4);
header[8] = 8;
header[9] = 2;
const scanlines = Buffer.alloc(240 * (1 + 320 * 3), 255);
for (let y = 0; y < 240; y++) scanlines[y * 961] = 0;
const original = Buffer.concat([
  Buffer.from("89504e470d0a1a0a", "hex"),
  chunk("IHDR", header),
  chunk("IDAT", deflateSync(scanlines)),
  chunk("IEND", Buffer.alloc(0)),
]);
await writeFile(join(images, "first.png"), original);
await writeFile(join(images, "untouched.png"), original);

// 320x240 RGBA: the top-left quarter has alpha 0, everything else is opaque.
function quarterAlphaPng() {
  const rgbaHeader = Buffer.from(header);
  rgbaHeader[9] = 6;
  const stride = 1 + 320 * 4;
  const rows = Buffer.alloc(240 * stride);
  for (let y = 0; y < 240; y++)
    for (let x = 0; x < 320; x++) {
      const at = y * stride + 1 + x * 4;
      rows.fill(128, at, at + 3);
      rows[at + 3] = x < 160 && y < 120 ? 0 : 255;
    }
  return Buffer.concat([
    original.subarray(0, 8),
    chunk("IHDR", rgbaHeader),
    chunk("IDAT", deflateSync(rows)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// 1920x1080 RGBA: alpha 0 on every pixel over magenta RGB. A renderer that
// ignored alpha would paint flat magenta instead of the checkerboard.
function alphaZeroPng() {
  const rgbaHeader = Buffer.from(header);
  rgbaHeader.writeUInt32BE(1920, 0);
  rgbaHeader.writeUInt32BE(1080, 4);
  rgbaHeader[9] = 6;
  const row = Buffer.alloc(1 + 1920 * 4);
  for (let x = 0; x < 1920; x++) row.set([255, 0, 255, 0], 1 + x * 4);
  const rows = Buffer.concat(Array.from({ length: 1080 }, () => row));
  return Buffer.concat([
    original.subarray(0, 8),
    chunk("IHDR", rgbaHeader),
    chunk("IDAT", deflateSync(rows)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// RGB PNG filled with `background`, with `fill` inside the inclusive box
// [left, top, right, bottom]. Rows are deflated a strip at a time, so a
// 9000x6000 image never needs its 162 MB of scanlines in memory at once.
async function rgbPng(width, height, background, box, fill) {
  const scanline = (crossesBox) => {
    const row = Buffer.alloc(1 + width * 3);
    for (let x = 0; x < width; x++)
      row.set(
        crossesBox && x >= box[0] && x <= box[2] ? fill : background,
        1 + x * 3,
      );
    return row;
  };
  const plain = scanline(false);
  const crossing = scanline(true);
  const deflate = createDeflate();
  const compressed = [];
  deflate.on("data", (bytes) => compressed.push(bytes));
  const finished = once(deflate, "end");
  for (let top = 0; top < height; top += 256) {
    const strip = [];
    for (let y = top; y < Math.min(height, top + 256); y++)
      strip.push(y >= box[1] && y <= box[3] ? crossing : plain);
    if (!deflate.write(Buffer.concat(strip))) await once(deflate, "drain");
  }
  deflate.end();
  await finished;
  const rgbHeader = Buffer.from(header);
  rgbHeader.writeUInt32BE(width, 0);
  rgbHeader.writeUInt32BE(height, 4);
  return Buffer.concat([
    original.subarray(0, 8),
    chunk("IHDR", rgbHeader),
    chunk("IDAT", Buffer.concat(compressed)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const LARGE_BACKGROUND = [232, 232, 232];
const LARGE_BOX = [2500, 1800, 2600, 1900];
const LARGE_BOX_FILL = [40, 80, 120];
async function createLargeSource(imageDirectory) {
  const file = join(imageDirectory, "large.png");
  const bytes = await rgbPng(
    9000,
    6000,
    LARGE_BACKGROUND,
    LARGE_BOX,
    LARGE_BOX_FILL,
  );
  await writeFile(file, bytes);
  return { file, bytes };
}

// Inserts an APP1 Exif segment whose only IFD0 entry is Orientation.
function withExifOrientation(jpeg, orientation) {
  assert.equal(jpeg.readUInt16BE(0), 0xffd8, "JPEG starts with SOI");
  const tiff = Buffer.from([
    ...[0x4d, 0x4d, 0x00, 0x2a, 0, 0, 0, 8],
    ...[0, 1],
    ...[0x01, 0x12, 0, 3, 0, 0, 0, 1, 0, orientation, 0, 0],
    ...[0, 0, 0, 0],
  ]);
  const payload = Buffer.concat([Buffer.from("Exif\0\0", "latin1"), tiff]);
  const marker = Buffer.alloc(4);
  marker.writeUInt16BE(0xffe1, 0);
  marker.writeUInt16BE(payload.length + 2, 2);
  return Buffer.concat([jpeg.subarray(0, 2), marker, payload, jpeg.subarray(2)]);
}

function cli(command, extra = [], resultDir = results, imageDirectory = images) {
  const args = [script, command, "--results", resultDir];
  if (command === "open")
    args.push(
      "--images",
      imageDirectory,
      "--question",
      "Keep or change this capture?",
      "--idle-timeout",
      "120",
      "--close-timeout",
      "2",
      "--no-browser",
    );
  const run = spawnSync("python3", [...args, ...extra], {
    encoding: "utf8",
    timeout: 15000,
    // status prints review.json, about 1.1 MB with 20,000 marks.
    maxBuffer: 64 * 1024 * 1024,
  });
  if (command !== "wait")
    assert.equal(run.status, 0, `${command}: ${run.stdout} ${run.stderr}`);
  return run;
}
async function verdict(resultDir = results) {
  return JSON.parse(await readFile(join(resultDir, "review.json"), "utf8"));
}
function readStatus(resultDir = results) {
  return JSON.parse(cli("status", [], resultDir).stdout);
}
function until(predicate, description, timeout = 10000) {
  const deadline = Date.now() + timeout;
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (Date.now() >= deadline) {
        reject(new Error(`Timed out: ${description}`));
        return;
      }
      Promise.resolve()
        .then(predicate)
        .then((satisfied) => {
          if (satisfied) resolve();
          else setTimeout(poll, 30);
        }, reject);
    };
    poll();
  });
}
// UI steps must run one after another; chain them instead of awaiting in a loop.
function inSequence(items, step) {
  return items.reduce(
    (chain, item) => chain.then(() => step(item)),
    Promise.resolve(),
  );
}
async function observed(predicate, description, timeout = 1000) {
  try {
    await until(predicate, description, timeout);
    return true;
  } catch {
    return false;
  }
}
function apiUrl(opened, path) {
  const base = opened.url.endsWith("/") ? opened.url : `${opened.url}/`;
  return new URL(`api/${path}`, base);
}
function boundaryMarks() {
  const singlePointPen = {
    type: "pen",
    color: "#ef4444",
    width: 1,
    points: [[4, 4]],
  };
  return [
    ...Array.from({ length: 19_999 }, () => ({ ...singlePointPen })),
    {
      type: "pen",
      color: "#ef4444",
      width: 1,
      points: [
        [20, 60],
        [200, 60],
      ],
    },
  ];
}
async function seedBoundary(opened, resultDir, marks) {
  await prepareOverHttp(opened, 0, 320, 240);
  const response = await fetch(apiUrl(opened, "save"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index: 0, revision: 1, notes: "", markup: marks }),
  });
  const acknowledgement = await response.json();
  assert.equal(response.status, 200, JSON.stringify(acknowledgement));
  assert.equal(acknowledgement.revision, 1);
  const saved = (await verdict(resultDir)).images[0].markup;
  assert.equal(saved.length, 20_000, "HTTP seed is exactly at the mark limit");
  assert.deepEqual(saved, marks, "HTTP seed accepts valid durable marks");
  return saved;
}
async function closeViaApi(opened) {
  try {
    await fetch(apiUrl(opened, "close"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ snapshots: [] }),
    });
  } catch {}
}
function watcher(resultDir = results) {
  const child = spawn("python3", [script, "wait", "--results", resultDir], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (bytes) => {
    stdout += bytes;
  });
  child.stderr.on("data", (bytes) => {
    stderr += bytes;
  });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("Watcher did not return"));
    }, 15000);
    child.on("error", reject);
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout: stdout.trim(), stderr });
    });
  });
}
async function pingAnswers(opened) {
  try {
    const response = await fetch(apiUrl(opened, "ping"), {
      signal: AbortSignal.timeout(2000),
    });
    const body = await response.json();
    return body.alive === true && body.session_id === opened.session_id;
  } catch {
    return false;
  }
}
function portAccepts(port) {
  return new Promise((resolve) => {
    const socket = connect({ host: "127.0.0.1", port });
    socket.setTimeout(2000, () => {
      socket.destroy();
      resolve(false);
    });
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
  });
}
// A live server answers ping, owns runtime.json and listens; a stopped one does none of these.
async function leakControl(opened, expectLive) {
  const port = Number(new URL(opened.url).port);
  const found = {
    ping: await pingAnswers(opened),
    runtime: existsSync(join(opened.results, "runtime.json")),
    port: await portAccepts(port),
  };
  assert.deepEqual(
    found,
    { ping: expectLive, runtime: expectLive, port: expectLive },
    `${expectLive ? "live" : "stopped"} server on port ${port}`,
  );
}
async function serverStopped(opened) {
  try {
    await leakControl(opened, false);
    return true;
  } catch {
    return false;
  }
}
async function setColor(page, color) {
  await page.locator("#color").evaluate((input, value) => {
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, color);
}
async function draw(page, tool, from, to) {
  await page.getByRole("button", { name: tool, exact: true }).click();
  const canvas = page.locator("#canvas");
  const box = await canvas.boundingBox();
  assert.ok(box, "Visible canvas");
  const [width, height] = await canvas.evaluate((element) => [
    element.width,
    element.height,
  ]);
  const x = (value) => box.x + (value / width) * box.width;
  const y = (value) => box.y + (value / height) * box.height;
  await page.mouse.move(x(from[0]), y(from[1]));
  await page.mouse.down();
  await page.mouse.move(x(to[0]), y(to[1]), { steps: 8 });
  await page.mouse.up();
}
async function pngPixels(page, path, points) {
  const png = (await readFile(path)).toString("base64");
  // Browser PNG decoder is independent of the application's PNG writer.
  return page.evaluate(
    async ({ png: pngBase64, points: pixelPoints }) => {
      const image = new Image();
      image.src = `data:image/png;base64,${pngBase64}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.width;
      canvas.height = image.height;
      const pixelContext = canvas.getContext("2d");
      pixelContext.drawImage(image, 0, 0);
      return {
        width: image.width,
        height: image.height,
        pixels: pixelPoints.map(([x, y]) =>
          Array.from(pixelContext.getImageData(x, y, 1, 1).data),
        ),
      };
    },
    { png, points },
  );
}
async function screenshotLuma(page, clip) {
  const png = (await page.screenshot({ clip })).toString("base64");
  return page.evaluate(async (pngBase64) => {
    const image = new Image();
    image.src = `data:image/png;base64,${pngBase64}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const pixelContext = canvas.getContext("2d");
    pixelContext.drawImage(image, 0, 0);
    const { data } = pixelContext.getImageData(0, 0, image.width, image.height);
    const luma = [];
    for (let at = 0; at < data.length; at += 4)
      luma.push(0.299 * data[at] + 0.587 * data[at + 1] + 0.114 * data[at + 2]);
    return luma;
  }, png);
}
// The live layout viewport. In fullscreen it is the screen, whose size an
// engine may choose itself instead of the configured viewport.
function liveViewport(page) {
  return page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
}
// The revision in a PNG's visual-review tEXt chunk, or null without the file or the chunk.
async function readPngRevision(path) {
  if (!existsSync(path)) return null;
  const png = await readFile(path);
  for (let offset = 8; offset + 12 <= png.length; ) {
    const size = png.readUInt32BE(offset);
    const type = png.subarray(offset + 4, offset + 8).toString("ascii");
    if (type === "tEXt") {
      const [key, value] = png
        .subarray(offset + 8, offset + 8 + size)
        .toString("latin1")
        .split("\0");
      if (key === "visual-review-markup-revision") return Number(value);
    }
    offset += size + 12;
  }
  return null;
}
async function pngRevision(path) {
  const revision = await readPngRevision(path);
  assert.ok(
    revision !== null,
    "Marked PNG must carry its rendered revision as PNG tEXt",
  );
  return revision;
}
// Polls the CLI status until the item's marked PNG is current: the status
// calls it fresh at the markup revision and the file on disk carries that
// revision. Only then may a test read the PNG of a live session.
async function markedCurrent(resultDir, index, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const entry = readStatus(resultDir).images[index];
    if (
      entry.markup.length &&
      entry.png_stale === false &&
      entry.png_revision === entry.markup_revision &&
      typeof entry.marked === "string" &&
      (await readPngRevision(join(resultDir, entry.marked))) ===
        entry.markup_revision
    )
      return entry;
    if (Date.now() >= deadline)
      assert.fail(
        `marked PNG of item ${index} not current after ${timeout} ms: ` +
          JSON.stringify({
            markup_revision: entry.markup_revision,
            png_revision: entry.png_revision,
            png_stale: entry.png_stale,
          }),
      );
    await sleep(150);
  }
}
// Reports an image's decoded size the way the page does, so HTTP seeds may save markup.
async function prepareOverHttp(opened, index, width, height) {
  const response = await fetch(apiUrl(opened, "prepare"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index, width, height }),
  });
  const prepared = await response.json();
  assert.equal(response.status, 200, JSON.stringify(prepared));
  assert.equal(prepared.ready, true);
  return prepared;
}
// Undoes one PNG row filter in place (PNG specification, section 9).
function unfilterRow(filter, row, previous, bpp) {
  if (filter === 0) return;
  assert.ok(filter >= 1 && filter <= 4, `PNG filter type ${filter}`);
  for (let i = 0; i < row.length; i++) {
    const left = i >= bpp ? row[i - bpp] : 0;
    const up = previous[i];
    let predicted;
    if (filter === 1) predicted = left;
    else if (filter === 2) predicted = up;
    else if (filter === 3) predicted = (left + up) >> 1;
    else {
      const upLeft = i >= bpp ? previous[i - bpp] : 0;
      const estimate = left + up - upLeft;
      const toLeft = Math.abs(estimate - left);
      const toUp = Math.abs(estimate - up);
      const toUpLeft = Math.abs(estimate - upLeft);
      predicted =
        toLeft <= toUp && toLeft <= toUpLeft
          ? left
          : toUp <= toUpLeft
            ? up
            : upLeft;
    }
    row[i] = (row[i] + predicted) & 255;
  }
}
// Decodes an 8-bit, non-interlaced RGB or RGBA PNG with zlib alone, handing
// each unfiltered row to onRow(y, row, channels). Independent of the browser
// that encoded the PNG, and it holds only two rows at a time.
async function forEachPngRow(path, onRow) {
  const png = await readFile(path);
  assert.equal(png.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
  const width = png.readUInt32BE(16);
  const height = png.readUInt32BE(20);
  const [depth, colorType, , , interlace] = png.subarray(24, 29);
  assert.ok(
    depth === 8 && (colorType === 2 || colorType === 6) && interlace === 0,
    `unsupported PNG layout: depth ${depth}, color type ${colorType}, interlace ${interlace}`,
  );
  const channels = colorType === 6 ? 4 : 3;
  const stride = width * channels;
  const compressed = [];
  for (let offset = 8; offset + 12 <= png.length; ) {
    const size = png.readUInt32BE(offset);
    if (png.toString("latin1", offset + 4, offset + 8) === "IDAT")
      compressed.push(png.subarray(offset + 8, offset + 8 + size));
    offset += size + 12;
  }
  let previous = Buffer.alloc(stride);
  let pending = Buffer.alloc(0);
  let y = 0;
  const inflate = createInflate({ chunkSize: 1 << 20 });
  inflate.on("data", (bytes) => {
    pending = pending.length ? Buffer.concat([pending, bytes]) : bytes;
    while (pending.length > stride) {
      const row = Buffer.from(pending.subarray(1, stride + 1));
      unfilterRow(pending[0], row, previous, channels);
      pending = pending.subarray(stride + 1);
      onRow(y, row, channels);
      previous = row;
      y += 1;
    }
  });
  const finished = once(inflate, "end");
  inflate.end(Buffer.concat(compressed));
  await finished;
  assert.equal(y, height, "every PNG row decoded");
  return { width, height };
}
async function runBoundaryCase(
  testCase,
  { context, root: caseRoot, errors, boundaryFailures },
) {
  const resultDir = join(caseRoot, `boundary-${testCase.tool.toLowerCase()}`);
  await mkdir(resultDir);
  let opened = JSON.parse(cli("open", [], resultDir).stdout);
  await leakControl(opened, true);
  const marks = boundaryMarks();
  let sessionEnded = false;
  let page;
  try {
    page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    const savedMarks = await seedBoundary(opened, resultDir, marks);
    await page.goto(opened.url);
    await page.locator("#canvas").waitFor({ state: "visible" });
    await until(
      async () =>
        page
          .locator("#canvas")
          .evaluate((canvas) => canvas.width === 320 && canvas.height === 240),
      "boundary image loaded",
    );
    assert.equal(
      (await verdict(resultDir)).images[0].markup.length,
      20_000,
    );
    // The seed left no PNG, so the page renders and uploads one after prepare.
    await markedCurrent(resultDir, 0);
    const acceptedControl = await pngPixels(
      page,
      join(resultDir, "first.marked.png"),
      [[100, 60]],
    );
    assert.ok(
      acceptedControl.pixels[0][0] > 180 &&
        acceptedControl.pixels[0][1] < 180 &&
        acceptedControl.pixels[0][2] < 180,
      "HTTP-seeded accepted pen is visible in the durable PNG",
    );
    await draw(page, testCase.tool, testCase.from, testCase.to);
    assert.ok(
      await page.locator("#error-message").isVisible(),
      `${testCase.tool} rejection is visible at the saved-mark limit`,
    );
    assert.match(
      await page.locator("#error-message").innerText(),
      /20[,.\s]?000/,
    );
    assert.deepEqual(
      (await verdict(resultDir)).images[0].markup,
      savedMarks,
      `${testCase.tool} rejection leaves durable markup unchanged`,
    );
    const previewPixel = await page
      .locator("#canvas")
      .evaluate(
        (canvas, [x, y]) =>
          Array.from(canvas.getContext("2d").getImageData(x, y, 1, 1).data),
        testCase.previewPixel,
      );
    if (
      previewPixel.some(
        (channel, index) => channel !== [255, 255, 255, 255][index],
      )
    )
      boundaryFailures.push(
        `${testCase.tool}: rejected shape preview remains on the canvas`,
      );

    // Each 20,000-mark save validates every mark, and an export can run
    // beside it; these windows bound a stall, not the expected save time.
    const durableWithin = 15_000;
    const autosavedNote = `${testCase.tool} rejection autosave remains usable`;
    await page.locator("#notes").fill(autosavedNote);
    if (
      !(await observed(
        async () =>
          (await verdict(resultDir)).images[0].notes === autosavedNote,
        `${testCase.tool} autosave after rejection`,
        durableWithin,
      ))
    )
      boundaryFailures.push(`${testCase.tool}: note autosave was blocked`);

    await page.getByRole("button", { name: "Next", exact: true }).click();
    const navigated = await observed(
      async () => (await page.locator("#index").inputValue()) === "2",
      `${testCase.tool} navigation after rejection`,
    );
    if (!navigated) {
      boundaryFailures.push(
        `${testCase.tool}: navigation remained trapped`,
      );
    } else {
      await page
        .getByRole("button", { name: "Previous", exact: true })
        .click();
      const returned = await observed(
        async () => (await page.locator("#index").inputValue()) === "1",
        `${testCase.tool} return navigation`,
      );
      if (!returned)
        boundaryFailures.push(
          `${testCase.tool}: could not return to boundary image`,
        );
      else await page.locator("#canvas").waitFor({ state: "visible" });
    }

    await page.getByRole("button", { name: "Undo", exact: true }).click();
    const undoWorked = await observed(
      async () =>
        (await verdict(resultDir)).images[0].markup.length === 19_999,
      `${testCase.tool} Undo after rejected shape`,
      durableWithin,
    );
    if (!undoWorked)
      boundaryFailures.push(`${testCase.tool}: Undo remained trapped`);

    await page.getByRole("button", { name: "Clear", exact: true }).click();
    const clearWorked = await observed(
      async () => (await verdict(resultDir)).images[0].markup.length === 0,
      `${testCase.tool} Clear after rejected shape`,
      durableWithin,
    );
    if (!clearWorked)
      boundaryFailures.push(`${testCase.tool}: Clear remained trapped`);

    if (clearWorked) {
      const terminalNote = `${testCase.tool} boundary recovery persisted`;
      await page.locator("#notes").fill(terminalNote);
      if (testCase.terminal === "done") {
        const doneWatch = watcher(resultDir);
        await page.getByRole("button", { name: "Done", exact: true }).click();
        assert.deepEqual(await doneWatch, {
          code: 0,
          stdout: "done",
          stderr: "",
        });
      } else {
        const closeWatch = watcher(resultDir);
        await page.close({ runBeforeUnload: true });
        assert.deepEqual(await closeWatch, {
          code: 10,
          stdout: "closed",
          stderr: "",
        });
      }
      sessionEnded = true;
      const completed = await verdict(resultDir);
      assert.equal(completed.images[0].notes, terminalNote);
      assert.equal(completed.images[0].markup.length, 0);
    }
  } finally {
    if (!sessionEnded) await closeViaApi(opened);
    if (page) await page.close().catch(() => {});
  }
  if (
    !(await observed(
      () => serverStopped(opened),
      `${testCase.tool} boundary server cleanup`,
      3000,
    ))
  )
    boundaryFailures.push(`${testCase.tool}: boundary server did not stop`);
}

const testFocus = process.env.VISUAL_REVIEW_TEST_FOCUS ?? "all";
let browser;
try {
  assert.ok(
    [
      "all",
      "shapes",
      "dimensions",
      "empty-pen",
      "source-changed",
      "viewer",
      "video",
      "lifecycle",
    ].includes(testFocus),
    "VISUAL_REVIEW_TEST_FOCUS must be all, shapes, dimensions, " +
      "empty-pen, source-changed, viewer, video, or lifecycle",
  );
  const viewportSize = { width: 1280, height: 900 };
  browser = await engines[options.browser].launch({
    headless: true,
    // Fullscreen fills the screen, which headless Firefox sizes from these
    // variables. The checks expect a screen the size of the viewport.
    env: {
      ...process.env,
      MOZ_HEADLESS_WIDTH: String(viewportSize.width),
      MOZ_HEADLESS_HEIGHT: String(viewportSize.height),
    },
  });
  const context = await browser.newContext({ viewport: viewportSize });
  let opened;
  let page;
  const errors = [];
  const boundaryFailures = [];
  if (testFocus === "all") {
    opened = JSON.parse(cli("open").stdout);
    await leakControl(opened, true);
    page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(opened.url);
    await page.locator("#canvas").waitFor({ state: "visible" });
    await until(
      async () =>
        page.locator("#canvas").evaluate((canvas) => canvas.width === 320),
      "image loaded",
    );
    assert.equal(await page.locator("#index").inputValue(), "1");
    assert.equal(await page.locator("#index-total").innerText(), "/ 2");
    await page.locator("#notes").fill("Keep the lighting; move the label.");
    // Arrow keys in the notes must not change the selected image.
    await page.locator("#notes").press("ArrowRight");
    assert.equal(await page.locator("#index").inputValue(), "1");
    await page.locator("#notes").fill("😀".repeat(3001));
    assert.equal(
      await page.locator("#notes").inputValue(),
      "Keep the lighting; move the label.",
      "Reject a 6,002-code-unit replacement in full, never truncate notes",
    );
    assert.ok(
      await page.locator("#error-message").isVisible(),
      "Oversized notes show a visible error",
    );
    await setColor(page, "#ef4444");
    await draw(page, "Pen", [20, 30], [100, 30]);
    await setColor(page, "#22c55e");
    await draw(page, "Arrow", [20, 80], [100, 80]);
    await setColor(page, "#3b82f6");
    await draw(page, "Rectangle", [140, 30], [200, 90]);
    await until(
      async () => (await verdict()).images[0].markup.length === 3,
      "three drawing tools autosaved",
    );
    const savedMarkupBeforeRejectedLabel = (await verdict()).images[0].markup;
    await page.getByRole("button", { name: "Text", exact: true }).click();
    const textBox = await page.locator("#canvas").boundingBox();
    await page.mouse.click(
      textBox.x + (30 / 320) * textBox.width,
      textBox.y + (150 / 240) * textBox.height,
    );
    const saveBodies = [];
    await page.route("**/api/save", async (route) => {
      saveBodies.push(route.request().postDataJSON());
      await route.continue();
    });
    const saveCountBeforeRejectedLabel = saveBodies.length;
    const rejectedDraft = "i".repeat(240);
    assert.ok(
      await page.evaluate((text) => {
        const pixelContext = document.createElement("canvas").getContext("2d");
        pixelContext.font = "18px Arial, sans-serif";
        return pixelContext.measureText(text).width > 288;
      }, rejectedDraft),
      "240 narrow characters exceed the 288px canvas space at the 18px minimum",
    );
    await page.locator("#text-input").fill(rejectedDraft);
    await page.getByRole("button", { name: "Place text", exact: true }).click();
    assert.ok(
      await page.locator("#error-message").isVisible(),
      "Wrapped text over 240 codepoints shows a visible pre-admission error",
    );
    assert.match(
      await page.locator("#error-message").innerText(),
      /240/,
      "The pre-admission error explains the text limit",
    );
    assert.equal(await page.locator("#text-composer").isVisible(), true);
    assert.equal(
      await page.locator("#text-input").inputValue(),
      rejectedDraft,
      "Rejected draft remains intact for correction",
    );
    assert.deepEqual(
      (await verdict()).images[0].markup,
      savedMarkupBeforeRejectedLabel,
      "Rejected wrapped text does not change durable markup",
    );
    await page.locator("#text-input").fill("Fix");
    await page.getByRole("button", { name: "Place text", exact: true }).click();
    const correctedNote = "Shortened label saved after rejection.";
    await page.locator("#notes").fill(correctedNote);
    await until(
      async () => saveBodies.length > saveCountBeforeRejectedLabel,
      "shortened label save request",
    );
    const submittedText = saveBodies
      .slice(saveCountBeforeRejectedLabel)
      .flatMap((body) =>
        body.markup_delta
          .filter((patch) => patch.mark?.type === "text")
          .map((patch) => patch.mark.text),
      );
    assert.deepEqual(
      submittedText,
      ["Fix"],
      "Only the shortened label reaches the save API; rejected wrapped text is never admitted",
    );
    await until(async () => {
      const saved = await verdict();
      return (
        saved.images[0].notes === correctedNote &&
        saved.images[0].markup.length === 4
      );
    }, "shortened label and corrected note persist");
    const savedLabel = (await verdict()).images[0].markup.at(-1);
    assert.equal(savedLabel.type, "text");
    assert.equal(savedLabel.text, "Fix");
    await markedCurrent(results, 0);
    const exported = await pngPixels(page, join(results, "first.marked.png"), [
      [60, 30],
      [60, 80],
      [140, 60],
      [300, 220],
    ]);
    assert.deepEqual([exported.width, exported.height], [320, 240]);
    assert.ok(
      exported.pixels[0][0] > 180 &&
        exported.pixels[0][1] < 140 &&
        exported.pixels[0][2] < 140,
      `Red pen pixel: ${exported.pixels[0]}`,
    );
    assert.ok(
      exported.pixels[1][1] > 150 &&
        exported.pixels[1][0] < 140 &&
        exported.pixels[1][2] < 140,
      `Green arrow pixel: ${exported.pixels[1]}`,
    );
    assert.ok(
      exported.pixels[2][2] > 180 &&
        exported.pixels[2][0] < 140 &&
        exported.pixels[2][1] < 160,
      `Blue rectangle pixel: ${exported.pixels[2]}`,
    );
    assert.deepEqual(
      exported.pixels[3],
      [255, 255, 255, 255],
      "Original outside markup remains white",
    );
    const textPixels = await pngPixels(
      page,
      join(results, "first.marked.png"),
      Array.from({ length: 3000 }, (_, i) => [
        30 + (i % 60),
        125 + Math.floor(i / 60),
      ]),
    );
    assert.ok(
      textPixels.pixels.some((pixel) => pixel[1] < 180),
      "Text glyphs appear in exported PNG",
    );
    await page.getByRole("button", { name: "Undo", exact: true }).click();
    await until(
      async () => (await verdict()).images[0].markup.length === 3,
      "undo removes the corrected text label",
    );
    await page.getByRole("button", { name: "Text", exact: true }).click();
    const finalTextBox = await page.locator("#canvas").boundingBox();
    await page.mouse.click(
      finalTextBox.x + (30 / 320) * finalTextBox.width,
      finalTextBox.y + (150 / 240) * finalTextBox.height,
    );
    await page.locator("#text-input").fill("Fix");
    await page.getByRole("button", { name: "Place text", exact: true }).click();
    await until(async () => {
      const markup = (await verdict()).images[0].markup;
      return markup.length === 4 && markup.at(-1).text === "Fix";
    }, "valid shortened label remains available for Done");
    await page.locator("#canvas").focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("#index").inputValue(), "2");
    await page.locator("#notes").fill("Leave this untouched.");
    await page.getByRole("button", { name: "Pen", exact: true }).click();
    const doneWatch = watcher();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    assert.deepEqual(await doneWatch, { code: 0, stdout: "done", stderr: "" });
    const done = JSON.parse(cli("results").stdout);
    assert.equal(done.done, true);
    assert.equal(done.images[0].notes, correctedNote);
    assert.equal(done.images[1].notes, "Leave this untouched.");
    assert.equal(done.images[0].markup.length, 4);
    assert.equal(done.images[0].markup.at(-1).text, "Fix");
    assert.equal(
      done.images[0].png_stale,
      false,
      "Done results never silently return a stale PNG",
    );
    assert.equal(done.images[0].png_revision, done.images[0].markup_revision);
    assert.ok(done.images[0].png_revision > 0);
    assert.equal(
      await pngRevision(join(results, "first.marked.png")),
      done.images[0].markup_revision,
    );
    assert.equal(done.images[1].marked, null);
    assert.ok(
      !(await readdir(results)).includes("untouched.marked.png"),
      "Untouched negative control has no PNG",
    );
    assert.deepEqual(
      await readFile(join(images, "first.png")),
      original,
      "Original bytes unchanged",
    );
    assert.deepEqual(
      await readFile(join(images, "untouched.png")),
      original,
      "Untouched original unchanged",
    );
    await until(() => serverStopped(opened), "Done cleanup");
    await page.close();
    assert.deepEqual(errors, [], "No browser errors");
    console.log(
      "PASS carousel, notes, four tools, wrapped-label rejection and correction, PNG pixels, undo, Done, untouched control, original immutability",
    );

    // A new session restores editable markup and re-uploads a marked PNG
    // removed while no server ran; Clear removes the marked artifact.
    const finishedEntry = readStatus().images[0];
    await rm(join(results, "first.marked.png"));
    opened = JSON.parse(cli("open").stdout);
    await leakControl(opened, true);
    page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    const resumedUploads = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname.endsWith("/api/marked/0"))
        resumedUploads.push(url.searchParams.get("revision"));
    });
    const resumedPrepare = page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/api/prepare"),
    );
    await page.goto(opened.url);
    await until(
      async () =>
        page.locator("#canvas").evaluate((canvas) => canvas.width === 320),
      "resumed image",
    );
    const resumedPrepared = await (await resumedPrepare).json();
    assert.deepEqual(
      [
        resumedPrepared.png_stale,
        resumedPrepared.png_revision,
        resumedPrepared.markup_revision,
      ],
      [true, finishedEntry.markup_revision, finishedEntry.markup_revision],
      "prepare reports the missing PNG as stale at the saved markup revision",
    );
    const reuploaded = await markedCurrent(results, 0);
    assert.equal(reuploaded.markup_revision, finishedEntry.markup_revision);
    // Playwright may report the request after the CLI already sees its result.
    await until(() => resumedUploads.length > 0, "the upload request event");
    assert.deepEqual(
      resumedUploads,
      [String(finishedEntry.markup_revision)],
      "the page uploads once, at the saved markup revision",
    );
    const rerendered = await pngPixels(page, join(results, "first.marked.png"), [
      [60, 30],
      [300, 220],
    ]);
    assert.ok(
      rerendered.pixels[0][0] > 180 &&
        rerendered.pixels[0][1] < 140 &&
        rerendered.pixels[0][2] < 140,
      `The re-uploaded PNG holds the saved red pen: ${rerendered.pixels[0]}`,
    );
    assert.deepEqual(rerendered.pixels[1], [255, 255, 255, 255]);
    assert.equal(await page.locator("#notes").inputValue(), correctedNote);
    await page.getByRole("button", { name: "Clear", exact: true }).click();
    await until(
      async () => !(await readdir(results)).includes("first.marked.png"),
      "clear removes PNG",
    );
    // Deliberately do not wait for debounce: normal close must flush the final note.
    await page
      .locator("#notes")
      .fill("Immediate close preserves this final note.");
    await setColor(page, "#ef4444");
    await draw(page, "Pen", [20, 30], [100, 30]);
    const leavePrompts = [];
    page.on("dialog", (dialog) => {
      leavePrompts.push(dialog.type());
      dialog.accept().catch(() => {});
    });
    const closeWatch = watcher();
    await page.close({ runBeforeUnload: true });
    assert.deepEqual(await closeWatch, {
      code: 10,
      stdout: "closed",
      stderr: "",
    });
    assert.deepEqual(
      leavePrompts,
      ["beforeunload"],
      "a close with a debounced save pending did not ask first",
    );
    const closed = JSON.parse(cli("results").stdout);
    assert.equal(
      closed.images[0].notes,
      "Immediate close preserves this final note.",
    );
    assert.equal(
      closed.images[0].markup.length,
      1,
      "Immediate close preserves the final stroke",
    );
    // The page may close before its upload, so the PNG may be stale; the
    // durable markup is authoritative, and results judge the PNG on disk.
    const closedPath = join(results, "first.marked.png");
    const closedRevision = await readPngRevision(closedPath);
    assert.ok(closed.images[0].markup_revision > 0);
    assert.equal(
      closed.images[0].png_stale,
      closedRevision !== closed.images[0].markup_revision,
      `results freshness must match the PNG on disk (revision ${closedRevision})`,
    );
    await leakControl(opened, false);
    assert.deepEqual(
      (await readdir(results))
        .filter((name) => !lockFiles.has(name) && name !== "first.marked.png")
        .toSorted(),
      ["review.json"],
    );
    if (!closed.images[0].png_stale) {
      const decoder = await context.newPage();
      const closedPNG = await pngPixels(decoder, closedPath, [[60, 30]]);
      assert.ok(
        closedPNG.pixels[0][1] < 140,
        "A current PNG after close holds the final stroke",
      );
      await decoder.close();
    }
    console.log(
      "PASS resumed notes/markup, PNG re-upload after resume, Clear, close-before-debounce note/stroke, closed watcher, cleanup",
    );

    opened = JSON.parse(cli("open").stdout);
    await leakControl(opened, true);
    page = await context.newPage();
    await page.goto(opened.url);
    await page.locator("#canvas").waitFor({ state: "visible" });
    await page.getByRole("button", { name: "Clear", exact: true }).click();
    await until(
      async () => !(await readdir(results)).includes("first.marked.png"),
      "clear before long stroke",
    );
    await page.getByRole("button", { name: "Pen", exact: true }).click();
    const longBox = await page.locator("#canvas").boundingBox();
    const lx = (value) => longBox.x + (value / 320) * longBox.width;
    const ly = (value) => longBox.y + (value / 240) * longBox.height;
    await page.mouse.move(lx(30), ly(120));
    await page.mouse.down();
    await Array.from({ length: 619 }, (_, offset) => offset + 1).reduce(
      (moves, index) =>
        moves.then(() =>
          page.mouse.move(
            lx(30 + (index % 2) * 6),
            ly(120 + (index % 17) * 1.7),
          ),
        ),
      Promise.resolve(),
    );
    await page.mouse.move(lx(100), ly(30));
    await page.mouse.up();
    await until(async () => {
      const marks = (await verdict()).images[0].markup;
      const endpoint = marks.at(-1)?.points?.at(-1);
      return (
        marks.length >= 2 &&
        endpoint &&
        Math.abs(endpoint[0] - 100) <= 1 &&
        Math.abs(endpoint[1] - 30) <= 1
      );
    }, "long stroke final endpoint persisted after segmentation");
    const segments = (await verdict()).images[0].markup;
    assert.ok(segments.every((mark) => mark.type === "pen"));
    assert.ok(
      segments.reduce((sum, segment) => sum + segment.points.length, 0) >= 600,
      "620 distinct cursor positions must not stop at a 512-point cap",
    );
    const last = segments.at(-1).points.at(-1);
    assert.ok(
      Math.abs(last[0] - 100) <= 1 && Math.abs(last[1] - 30) <= 1,
      `Final stroke endpoint lost: ${last}`,
    );
    for (let index = 1; index < segments.length; index++) {
      assert.deepEqual(
        segments[index].points[0],
        segments[index - 1].points.at(-1),
        "Segments must share their join point",
      );
    }
    await page.getByRole("button", { name: "Undo", exact: true }).click();
    await until(
      async () => (await verdict()).images[0].markup.length === 0,
      "one Undo removes the whole gesture",
    );
    assert.ok(
      !(await readdir(results)).includes("first.marked.png"),
      "Undo removes all chained markup pixels",
    );
    const longWatch = watcher();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    assert.deepEqual(await longWatch, { code: 0, stdout: "done", stderr: "" });
    await leakControl(opened, false);
    await page.close();
    console.log(
      "PASS >512-point lossless stroke, segment continuity, final endpoint, one-gesture Undo",
    );

    opened = JSON.parse(cli("open").stdout);
    page = await context.newPage();
    await page.goto(opened.url);
    await page.locator("#canvas").waitFor({ state: "visible" });
    await page.locator("#notes").fill("Frozen Done snapshot");
    let releaseDone;
    let enteredDone;
    const doneGate = new Promise((resolve) => {
      releaseDone = resolve;
    });
    const doneEntered = new Promise((resolve) => {
      enteredDone = resolve;
    });
    await page.route("**/api/done", async (route) => {
      enteredDone();
      await doneGate;
      await route.continue();
    });
    const barrierWatch = watcher();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await doneEntered;
    try {
      const noteBox = await page.locator("#notes").boundingBox();
      await page.mouse.click(noteBox.x + 12, noteBox.y + 12);
      await page.keyboard.type("LATE");
      assert.equal(
        await page.locator("#notes").inputValue(),
        "Frozen Done snapshot",
        "Done must reject reviewer edits after its save barrier starts",
      );
    } finally {
      releaseDone();
    }
    assert.deepEqual(await barrierWatch, {
      code: 0,
      stdout: "done",
      stderr: "",
    });
    assert.equal((await verdict()).images[0].notes, "Frozen Done snapshot");
    await leakControl(opened, false);
    await page.close();
    console.log(
      "PASS Done admission barrier rejects edits before terminal acknowledgement",
    );

    opened = JSON.parse(cli("open").stdout);
    page = await context.newPage();
    await page.goto(opened.url);
    await page.locator("#canvas").waitFor({ state: "visible" });
    if (
      await page.getByRole("button", { name: "Clear", exact: true }).isEnabled()
    ) {
      await page.getByRole("button", { name: "Clear", exact: true }).click();
    }
    await until(
      async () => (await verdict()).images[0].markup.length === 0,
      "clear before drag race",
    );
    let releaseSave;
    let enteredSave;
    let firstSave = true;
    const saveGate = new Promise((resolve) => {
      releaseSave = resolve;
    });
    const saveEntered = new Promise((resolve) => {
      enteredSave = resolve;
    });
    await page.route("**/api/save", async (route) => {
      if (firstSave) {
        firstSave = false;
        enteredSave();
        await saveGate;
      }
      await route.continue();
    });
    await page.locator("#notes").fill("Queued first note");
    await saveEntered;
    await page.locator("#notes").fill("Queued final note");
    // Wait past the 1 s notes debounce so the save is queued before the drag.
    await page.evaluate(
      () => new Promise((resolve) => setTimeout(resolve, 1250)),
    );
    await page.getByRole("button", { name: "Arrow", exact: true }).click();
    const dragBox = await page.locator("#canvas").boundingBox();
    const dx = (value) => dragBox.x + (value / 320) * dragBox.width;
    const dy = (value) => dragBox.y + (value / 240) * dragBox.height;
    await page.mouse.move(dx(20), dy(50));
    await page.mouse.down();
    await page.mouse.move(dx(60), dy(50));
    releaseSave();
    await until(
      async () => (await verdict()).images[0].notes === "Queued final note",
      "queued save executes during held shape",
    );
    await page.mouse.move(dx(180), dy(50));
    await page.mouse.up();
    const dragWatch = watcher();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    assert.deepEqual(await dragWatch, { code: 0, stdout: "done", stderr: "" });
    const draggedEntry = (await verdict()).images[0];
    const dragged = draggedEntry.markup[0];
    assert.equal(
      dragged.x2,
      180,
      "An acknowledged in-progress shape must not lose its final endpoint",
    );
    assert.deepEqual(
      [draggedEntry.png_stale, draggedEntry.png_revision],
      [false, draggedEntry.markup_revision],
      "Done waits for the upload of the final markup",
    );
    const arrowPixels = await pngPixels(
      page,
      join(results, "first.marked.png"),
      [[150, 50]],
    );
    assert.ok(
      arrowPixels.pixels[0][1] < 200,
      "Final arrow extension must appear in saved pixels",
    );
    await leakControl(opened, false);
    await page.close();
    console.log(
      "PASS queued-save drag race preserves immutable final geometry and PNG endpoint",
    );
  }
  if (testFocus === "all" || testFocus === "empty-pen") {
    const emptyResults = join(root, "empty-pen-results");
    await mkdir(emptyResults);
    opened = JSON.parse(cli("open", [], emptyResults).stdout);
    await leakControl(opened, true);
    let emptySessionEnded = false;
    try {
      const emptyPen = {
        type: "pen",
        color: "#ef4444",
        width: 1,
        points: [],
      };
      await prepareOverHttp(opened, 0, 320, 240);
      const response = await fetch(apiUrl(opened, "save"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          index: 0,
          revision: 1,
          notes: "",
          markup: [emptyPen],
        }),
      });
      const acknowledgement = await response.json();
      assert.equal(response.status, 200, JSON.stringify(acknowledgement));
      assert.equal(acknowledgement.revision, 1);
      assert.deepEqual(
        (await verdict(emptyResults)).images[0].markup,
        [emptyPen],
        "The public save API durably accepts an empty pen path",
      );

      const emptyPage = await context.newPage();
      const emptyPageErrors = [];
      emptyPage.on("pageerror", (error) => {
        emptyPageErrors.push(error.message);
        errors.push(error.message);
      });
      try {
        await emptyPage.goto(opened.url);
        const canvas = emptyPage.locator("#canvas");
        await until(
          () => canvas.isVisible(),
          "resumed image remains visible with empty pen markup",
        );
        assert.deepEqual(
          emptyPageErrors,
          [],
          "Resuming empty pen markup raises no browser error",
        );

        const savedNote = "Resume, edit, and finish the empty-pen review.";
        await emptyPage.locator("#notes").fill(savedNote);
        await setColor(emptyPage, "#ef4444");
        await draw(emptyPage, "Pen", [20, 30], [100, 30]);
        await until(async () => {
          const saved = await verdict(emptyResults);
          return (
            saved.images[0].notes === savedNote &&
            saved.images[0].markup.length === 2
          );
        }, "resumed notes and drawing autosave after empty pen markup");

        const doneWatch = watcher(emptyResults);
        await emptyPage
          .getByRole("button", { name: "Done", exact: true })
          .click();
        assert.deepEqual(await doneWatch, {
          code: 0,
          stdout: "done",
          stderr: "",
        });
        emptySessionEnded = true;
        const completed = await verdict(emptyResults);
        assert.equal(completed.done, true);
        assert.equal(completed.images[0].notes, savedNote);
        assert.equal(completed.images[0].markup.length, 2);
        assert.deepEqual(completed.images[0].markup[0], emptyPen);
        assert.equal(completed.images[0].markup[1].type, "pen");
        assert.ok(completed.images[0].markup[1].points.length >= 2);
        assert.equal(completed.images[0].png_stale, false);
        assert.equal(
          completed.images[0].png_revision,
          completed.images[0].markup_revision,
        );
        const pixels = await pngPixels(
          emptyPage,
          join(emptyResults, "first.marked.png"),
          [
            [60, 30],
            [300, 220],
          ],
        );
        assert.ok(
          pixels.pixels[0][0] > 180 &&
            pixels.pixels[0][1] < 140 &&
            pixels.pixels[0][2] < 140,
          `Resumed drawing appears red in the exported PNG: ${pixels.pixels[0]}`,
        );
        assert.deepEqual(
          pixels.pixels[1],
          [255, 255, 255, 255],
          "An untouched exported PNG pixel remains white",
        );
      } finally {
        await emptyPage.close().catch(() => {});
      }
    } finally {
      if (!emptySessionEnded) await closeViaApi(opened);
    }
    await leakControl(opened, false);
    console.log(
      "PASS empty-pen public API seed, resumed image, later edits, Done, and PNG pixels",
    );
  }

  if (testFocus === "all" || testFocus === "source-changed") {
    const sourceResults = join(root, "source-changed-results");
    await mkdir(sourceResults);
    const sourcePath = join(images, "first.png");
    assert.deepEqual(
      await readFile(sourcePath),
      original,
      "Source-change control starts from the original image",
    );
    const sourceOpened = JSON.parse(
      cli("open", [], sourceResults, images).stdout,
    );
    await leakControl(sourceOpened, true);
    let sourcePage;
    try {
      sourcePage = await context.newPage();
      sourcePage.on("pageerror", (error) => errors.push(error.message));
      await sourcePage.goto(sourceOpened.url);
      const sourceCanvas = sourcePage.locator("#canvas");
      await sourceCanvas.waitFor({ state: "visible" });
      await until(
        () =>
          sourceCanvas.evaluate(
            (canvas) => canvas.width === 320 && canvas.height === 240,
          ),
        "source-change image loaded",
      );

      const priorNote = "Keep this note with the original capture.";
      await sourcePage.locator("#notes").fill(priorNote);
      await setColor(sourcePage, "#ef4444");
      await draw(sourcePage, "Pen", [20, 30], [100, 30]);
      await until(async () => {
        const saved = await verdict(sourceResults);
        return (
          saved.images[0].notes === priorNote &&
          saved.images[0].markup.length === 1
        );
      }, "prior note and mark persist before replacement");
      await markedCurrent(sourceResults, 0);
      const durableBefore = await verdict(sourceResults);
      assert.equal(durableBefore.images[0].markup[0].type, "pen");
      assert.equal(
        durableBefore.images[0].png_revision,
        durableBefore.images[0].markup_revision,
      );
      const markedPath = join(sourceResults, "first.marked.png");
      const markedBefore = await readFile(markedPath);
      const priorPixel = await pngPixels(sourcePage, markedPath, [[60, 30]]);
      assert.ok(
        priorPixel.pixels[0][0] > 180 &&
          priorPixel.pixels[0][1] < 140 &&
          priorPixel.pixels[0][2] < 140,
        "The prior reviewer mark is present in the durable PNG",
      );

      const replacementBytes = await rgbPng(
        320,
        240,
        [32, 96, 192],
        [150, 100, 210, 160],
        [240, 160, 24],
      );
      const dimensions = (png) => [
        png.readUInt32BE(16),
        png.readUInt32BE(20),
      ];
      assert.deepEqual(
        dimensions(replacementBytes),
        dimensions(original),
        "Replacement image has the same pixel dimensions",
      );
      assert.notDeepEqual(
        replacementBytes,
        original,
        "Replacement image has different content",
      );
      await writeFile(sourcePath, replacementBytes);
      assert.deepEqual(
        await readFile(sourcePath),
        replacementBytes,
        "The source was replaced under its original name",
      );

      const attemptedNote = "Attempted edit after the source changed.";
      await until(
        async () =>
          (await sourcePage.locator("#save-status").getAttribute("data-kind")) ===
          "saved",
        "baseline note and stroke acknowledged before source-change edit",
      );
      const refusedSave = sourcePage.waitForResponse((response) => {
        const request = response.request();
        return (
          request.method() === "POST" &&
          new URL(response.url()).pathname.endsWith("/api/save")
        );
      });
      await sourcePage.locator("#notes").fill(attemptedNote);
      const response = await refusedSave;
      assert.ok(
        !response.ok(),
        `Changed-source save must be unacknowledged, got HTTP ${response.status()}`,
      );
      await until(
        () => sourcePage.locator("#error-message").isVisible(),
        "changed-source refusal is visible in the browser",
      );
      assert.match(
        await sourcePage.locator("#error-message").innerText(),
        /source changed since open/i,
        "The visible save error identifies the changed source",
      );
      assert.equal(
        await sourcePage.locator("#notes").inputValue(),
        attemptedNote,
        "The rejected edit remains available in the notes field",
      );
      assert.equal(
        await sourcePage.locator("#notes").isEnabled(),
        true,
        "The reviewer can still recover the rejected edit",
      );
      assert.deepEqual(
        await verdict(sourceResults),
        durableBefore,
        "A refused save leaves the durable review unchanged",
      );
      assert.deepEqual(
        await readFile(markedPath),
        markedBefore,
        "A refused save preserves the prior marked PNG byte-for-byte",
      );
      assert.deepEqual(
        await readFile(sourcePath),
        replacementBytes,
        "A refused save does not restore or rewrite the replacement source",
      );
      // A marked PNG of the right size and revision is still refused.
      const refusedUpload = await fetch(
        apiUrl(
          sourceOpened,
          `marked/0?revision=${durableBefore.images[0].markup_revision}`,
        ),
        { method: "POST", body: original },
      );
      const refusedUploadBody = await refusedUpload.json();
      assert.equal(refusedUpload.status, 400, JSON.stringify(refusedUploadBody));
      assert.match(refusedUploadBody.error, /source changed since open/i);
      assert.deepEqual(
        await readFile(markedPath),
        markedBefore,
        "A refused upload preserves the prior marked PNG byte-for-byte",
      );
      assert.deepEqual(await verdict(sourceResults), durableBefore);
      console.log(
        "PASS source-changed refusal of saves and uploads, recoverable UI draft, and durable artifact preservation",
      );
    } finally {
      await closeViaApi(sourceOpened);
      if (sourcePage) await sourcePage.close().catch(() => {});
      await writeFile(sourcePath, original);
    }
    assert.ok(
      await observed(
        () => serverStopped(sourceOpened),
        "source-change server cleanup",
        3000,
      ),
      "Source-change review server did not stop",
    );
  }

  if (testFocus === "all" || testFocus === "shapes") {
    const boundaryCases = [
      {
        tool: "Arrow",
        from: [20, 80],
        to: [100, 80],
        previewPixel: [60, 80],
        terminal: "done",
      },
      {
        tool: "Rectangle",
        from: [140, 30],
        to: [200, 90],
        previewPixel: [170, 30],
        terminal: "close",
      },
    ];
    await boundaryCases.reduce(
      (previousCase, testCase) =>
        previousCase.then(() =>
          runBoundaryCase(testCase, {
            context,
            root,
            errors,
            boundaryFailures,
          }),
        ),
      Promise.resolve(),
    );
    console.log(
      "EXERCISED 20,000-mark Arrow/Rectangle rejection, navigation, Undo/Clear, autosave, Done and close",
    );
  }

  if (testFocus === "all" || testFocus === "dimensions") {
    const largeImages = join(root, "large-images");
    const largeResults = join(root, "browser-large-image");
    await mkdir(largeImages);
    await mkdir(largeResults);
    const largeSource = await createLargeSource(largeImages);
    // Decoding and exporting 54 MP pauses the page's heartbeats, on a loaded
    // runner for longer than this suite's 2 s close timeout; this session
    // uses the 30 s default.
    opened = JSON.parse(
      cli("open", ["--close-timeout", "30"], largeResults, largeImages).stdout,
    );
    await leakControl(opened, true);
    page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    let ordinarySaveAboveBeaconLimit = false;
    const prepareBodies = [];
    const prepareResponses = [];
    const mediaResponses = [];
    page.on("request", (request) => {
      const path = new URL(request.url()).pathname;
      if (path.endsWith("/api/prepare"))
        prepareBodies.push(request.postDataJSON());
      if (
        path.endsWith("/api/save") &&
        Buffer.byteLength(request.postData() ?? "") > 56 * 1024
      )
        ordinarySaveAboveBeaconLimit = true;
    });
    page.on("response", (response) => {
      const path = new URL(response.url()).pathname;
      if (path.endsWith("/api/prepare")) prepareResponses.push(response);
      if (path.endsWith("/media/0")) mediaResponses.push(response);
    });
    let largeSessionEnded = false;
    try {
      await page.goto(opened.url);
      await until(
        async () => page.locator("#canvas").isVisible(),
        "large image opens",
        60_000,
      );
      const stored = readStatus(largeResults).images[0];
      assert.deepEqual(
        [stored.width, stored.height],
        [9000, 6000],
        "review.json records the size from the file header",
      );
      assert.ok(
        stored.width * stored.height > 50_000_000,
        "The source is a real image above 50 megapixels",
      );
      const media = mediaResponses.find((response) => response.status() === 200);
      assert.ok(media, "The page loads the image from media/0");
      assert.deepEqual(
        await media.body(),
        largeSource.bytes,
        "The page decodes the original file",
      );
      assert.deepEqual(
        prepareBodies,
        [{ index: 0, width: 9000, height: 6000 }],
        "prepare reports the size the browser decoded",
      );
      assert.deepEqual(await prepareResponses[0].json(), {
        ready: true,
        width: 9000,
        height: 6000,
        revision: 0,
        markup_revision: 0,
        png_revision: null,
        png_stale: false,
      });
      const [previewWidth, previewHeight] = await page
        .locator("#canvas")
        .evaluate((canvas) => [canvas.width, canvas.height]);
      assert.equal(
        previewWidth,
        2048,
        "The canvas shows a copy whose longest edge is 2048 px",
      );
      assert.ok(
        Math.abs(previewHeight - (6000 * 2048) / 9000) <= 1,
        `The canvas copy keeps the aspect ratio: ${previewHeight}`,
      );

      // Saves here can wait behind a 54 MP export; these bounds tolerate a
      // loaded runner and do not slow a passing run.
      const savedWithin = 60_000;
      await setColor(page, "#ef4444");
      await draw(page, "Pen", [100, 300], [500, 300]);
      const penDrawnAt = Date.now();
      await until(
        async () => (await verdict(largeResults)).images[0].markup.length === 1,
        "large-image stroke autosaves",
        savedWithin,
      );
      const largeCanvas = page.locator("#canvas");
      const box = await largeCanvas.boundingBox();
      const originalWidth = stored.width;
      const originalHeight = stored.height;
      const scaleX = originalWidth / previewWidth;
      const scaleY = originalHeight / previewHeight;
      const pen = (await verdict(largeResults)).images[0].markup[0];
      assert.equal(pen.type, "pen");
      // Firefox reports pointer positions in whole CSS pixels.
      const near = 2 + originalWidth / box.width;
      assert.ok(
        Math.abs(pen.points[0][0] - 100 * scaleX) <= near &&
          Math.abs(pen.points[0][1] - 300 * scaleY) <= near,
        `Stroke starts in original pixels: ${JSON.stringify(pen.points[0])}`,
      );
      assert.ok(
        Math.abs(pen.points.at(-1)[0] - 500 * scaleX) <= near &&
          Math.abs(pen.points.at(-1)[1] - 300 * scaleY) <= near,
        `Stroke endpoint remains in original pixels: ${JSON.stringify(pen.points.at(-1))}`,
      );
      assert.ok(
        Math.abs((pen.width * box.width) / originalWidth - 3) < 0.5,
        "Stroke width remains about three display pixels at large resolution",
      );
      // One full-resolution export: the page renders 9000x6000 pixels about
      // 1 s after the last markup change, then uploads the PNG.
      await markedCurrent(largeResults, 0, 120_000);
      const exportSeconds = (Date.now() - penDrawnAt) / 1000;
      console.log(
        `TIME ${options.browser}: 54 MP marked PNG current ${exportSeconds.toFixed(1)} s after the stroke (1 s upload delay included)`,
      );

      const textPreviewPoint = [250, 160];
      await page.getByRole("button", { name: "Text", exact: true }).click();
      await page.mouse.click(
        box.x + (textPreviewPoint[0] / previewWidth) * box.width,
        box.y + (textPreviewPoint[1] / previewHeight) * box.height,
      );
      await page.locator("#text-input").fill("Photo");
      await page.getByRole("button", { name: "Place text", exact: true }).click();
      await until(
        async () => (await verdict(largeResults)).images[0].markup.length === 2,
        "large-image text mask autosaves",
        savedWithin,
      );
      const textMark = (await verdict(largeResults)).images[0].markup[1];
      assert.equal(textMark.type, "text");
      assert.ok(
        Math.abs(textMark.x - textPreviewPoint[0] * scaleX) <= near &&
          Math.abs(textMark.y - textPreviewPoint[1] * scaleY) <= near,
        "Text origin is stored in original-image pixel coordinates",
      );
      assert.equal(
        textMark.mask.scale,
        scaleX,
        "The mask records original pixels per mask pixel",
      );
      assert.ok(
        textMark.mask.width < previewWidth &&
          textMark.mask.height < previewHeight,
        "Text mask remains at the canvas working resolution",
      );
      assert.ok(
        textMark.size > 220 &&
          Math.abs((textMark.size * box.width) / originalWidth - 22) < 2,
        "Text size uses uncapped original pixels while retaining its display size",
      );
      const largeNotes = "x".repeat(1800);
      const enteredLargeNotes = await page
        .locator("#notes")
        .evaluate((textarea, length) => {
          for (let index = 0; index < length; index += 1) {
            textarea.value += "x";
            textarea.dispatchEvent(new Event("input", { bubbles: true }));
          }
          return textarea.value;
        }, largeNotes.length);
      assert.equal(enteredLargeNotes, largeNotes);
      await until(
        async () => (await verdict(largeResults)).images[0].notes === largeNotes,
        "ordinary save drains history larger than the beacon transport limit",
        savedWithin,
      );
      assert.equal(
        ordinarySaveAboveBeaconLimit,
        true,
        "Large pending history uses uncapped ordinary save rather than keepalive",
      );
      await markedCurrent(largeResults, 0, 120_000);

      const largeWatch = watcher(largeResults);
      await page.getByRole("button", { name: "Done", exact: true }).click();
      assert.deepEqual(await largeWatch, {
        code: 0,
        stdout: "done",
        stderr: "",
      });
      largeSessionEnded = true;
      const largeVerdict = await verdict(largeResults);
      assert.equal(largeVerdict.images[0].markup.length, 2);
      assert.equal(largeVerdict.images[0].notes, largeNotes);
      assert.deepEqual(
        [largeVerdict.images[0].png_stale, largeVerdict.images[0].png_revision],
        [false, largeVerdict.images[0].markup_revision],
        "Full-resolution marked output is current at the saved revision",
      );
      const strokePoint = [
        Math.round(300 * scaleX),
        Math.round(300 * scaleY),
      ];
      const textBox = [
        Math.round(textPreviewPoint[0] * scaleX),
        Math.round(textPreviewPoint[1] * scaleY),
      ];
      // The source box has hard edges, so a full-resolution draw keeps
      // them exact; an upscaled 2048 px copy would blend the edge pixels.
      const probes = {
        stroke: strokePoint,
        boxCenter: [2550, 1850],
        boxLeftEdge: [LARGE_BOX[0], 1850],
        leftOfBox: [LARGE_BOX[0] - 1, 1850],
        boxBottomEdge: [2550, LARGE_BOX[3]],
        belowBox: [2550, LARGE_BOX[3] + 1],
      };
      const sampled = {};
      let textHasRed = false;
      const markedSize = await forEachPngRow(
        join(largeResults, "large.marked.png"),
        (y, row, channels) => {
          for (const [name, [x, probeY]] of Object.entries(probes))
            if (probeY === y)
              sampled[name] = Array.from(
                row.subarray(x * channels, x * channels + 3),
              );
          if (textHasRed || y < textBox[1] || y >= textBox[1] + 500) return;
          for (let x = textBox[0]; x < textBox[0] + 500; x++) {
            const at = x * channels;
            if (row[at] > 180 && row[at + 1] < 140 && row[at + 2] < 140) {
              textHasRed = true;
              return;
            }
          }
        },
      );
      assert.deepEqual(
        [markedSize.width, markedSize.height],
        [9000, 6000],
        "The marked PNG has the full display size",
      );
      assert.ok(
        sampled.stroke[0] > 180 &&
          sampled.stroke[1] < 140 &&
          sampled.stroke[2] < 140,
        `Pen appears at its original-pixel location: ${sampled.stroke}`,
      );
      assert.equal(
        textHasRed,
        true,
        "Scaled text mask is composited into the original-resolution PNG",
      );
      const close = (actual, expected) =>
        actual.every((value, channel) => Math.abs(value - expected[channel]) <= 3);
      for (const name of ["boxCenter", "boxLeftEdge", "boxBottomEdge"])
        assert.ok(
          close(sampled[name], LARGE_BOX_FILL),
          `${name} keeps the source color: ${sampled[name]}`,
        );
      for (const name of ["leftOfBox", "belowBox"])
        assert.ok(
          close(sampled[name], LARGE_BACKGROUND),
          `${name} keeps the source background: ${sampled[name]}`,
        );
      assert.deepEqual(
        await readFile(largeSource.file),
        largeSource.bytes,
        "Large original bytes remain unchanged",
      );
    } finally {
      if (!largeSessionEnded) await closeViaApi(opened);
      await page.close().catch(() => {});
    }
    await leakControl(opened, false);

    // A JPEG whose EXIF orientation turns it a quarter turn. The browser
    // shows it upright, so prepare and the marked PNG use the swapped size.
    const exifImages = join(root, "exif-images");
    const exifResults = join(root, "exif-results");
    await mkdir(exifImages);
    await mkdir(exifResults);
    const jpegMaker = await context.newPage();
    const jpeg = Buffer.from(
      await jpegMaker.evaluate(async () => {
        const source = document.createElement("canvas");
        source.width = 64;
        source.height = 32;
        const drawing = source.getContext("2d");
        drawing.fillStyle = "#ff0000";
        drawing.fillRect(0, 0, 32, 32);
        drawing.fillStyle = "#0000ff";
        drawing.fillRect(32, 0, 32, 32);
        const blob = await new Promise((resolve) =>
          source.toBlob(resolve, "image/jpeg", 0.95),
        );
        return Array.from(new Uint8Array(await blob.arrayBuffer()));
      }),
    );
    await jpegMaker.close();
    await writeFile(
      join(exifImages, "rotated.jpg"),
      withExifOrientation(jpeg, 6),
    );
    opened = JSON.parse(cli("open", [], exifResults, exifImages).stdout);
    await leakControl(opened, true);
    page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    const exifPrepares = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.endsWith("/api/prepare"))
        exifPrepares.push(request.postDataJSON());
    });
    let exifSessionEnded = false;
    try {
      await page.goto(opened.url);
      await until(
        async () =>
          page
            .locator("#canvas")
            .evaluate(
              (canvas) =>
                !canvas.hidden && canvas.width === 32 && canvas.height === 64,
            ),
        "rotated image shown upright",
      );
      const rotated = readStatus(exifResults).images[0];
      assert.deepEqual(
        [rotated.width, rotated.height],
        [64, 32],
        "review.json keeps the size from the JPEG header",
      );
      assert.deepEqual(
        exifPrepares,
        [{ index: 0, width: 32, height: 64 }],
        "prepare reports the size after EXIF orientation",
      );
      await setColor(page, "#22c55e");
      await draw(page, "Pen", [4, 30], [28, 30]);
      await until(
        async () => (await verdict(exifResults)).images[0].markup.length === 1,
        "rotated-image stroke saved",
      );
      const [, strokeY] = (await verdict(exifResults)).images[0].markup[0]
        .points[0];
      await markedCurrent(exifResults, 0);
      const rotatedPath = join(exifResults, "rotated.marked.png");
      const rotatedPng = await readFile(rotatedPath);
      assert.deepEqual(
        [rotatedPng.readUInt32BE(16), rotatedPng.readUInt32BE(20)],
        [32, 64],
        "The marked PNG has the display size",
      );
      // Orientation 6 turns the image clockwise: its red left half is on top.
      // The 3 px stroke covers the whole pixel row that holds its centre line.
      const upright = await pngPixels(page, rotatedPath, [
        [16, 8],
        [16, 56],
        [16, Math.floor(strokeY)],
      ]);
      const [top, bottom, stroke] = upright.pixels;
      assert.ok(
        top[0] > 180 && top[1] < 90 && top[2] < 90,
        `top of the upright image is red: ${top}`,
      );
      assert.ok(
        bottom[2] > 180 && bottom[0] < 90 && bottom[1] < 90,
        `bottom of the upright image is blue: ${bottom}`,
      );
      assert.ok(
        stroke[1] > 150 && stroke[0] < 120 && stroke[2] < 140,
        `the green stroke is where it was drawn: ${stroke}`,
      );
      const exifWatch = watcher(exifResults);
      await page.getByRole("button", { name: "Done", exact: true }).click();
      assert.deepEqual(await exifWatch, { code: 0, stdout: "done", stderr: "" });
      exifSessionEnded = true;
    } finally {
      if (!exifSessionEnded) await closeViaApi(opened);
      await page.close().catch(() => {});
    }
    await leakControl(opened, false);
    console.log(
      "PASS EXIF-rotated JPEG prepares and exports at its upright size",
    );
  }
  if (testFocus === "all" || testFocus === "viewer") {
    // Oracles: geometry comes from the canvas's rendered box and the
    // durable verdict, never from the page's own view state.
    const viewerImages = join(root, "viewer-images");
    const viewerResults = join(root, "viewer-results");
    await mkdir(viewerImages);
    await mkdir(viewerResults);
    await writeFile(join(viewerImages, "a-opaque.png"), original);
    await writeFile(join(viewerImages, "b-alpha-zero.png"), alphaZeroPng());
    await writeFile(
      join(viewerImages, "c-quarter-alpha.png"),
      quarterAlphaPng(),
    );
    opened = JSON.parse(cli("open", [], viewerResults, viewerImages).stdout);
    await leakControl(opened, true);
    page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.on("pageerror", (error) => errors.push(error.message));
    const markupSaves = [];
    page.on("request", (request) => {
      if (!new URL(request.url()).pathname.endsWith("/api/save")) return;
      const body = request.postDataJSON();
      if (body?.markup_delta?.length) markupSaves.push(body);
    });
    const checks = [];
    try {
      await page.goto(opened.url);
      await page.locator("#canvas").waitFor({ state: "visible" });
      const viewport = page.viewportSize();
      const canvasBox = async () => {
        const box = await page.locator("#canvas").boundingBox();
        assert.ok(box, "Visible canvas");
        return box;
      };
      const fillsViewport = async () => {
        const box = await page.locator(".viewer").boundingBox();
        const live = await liveViewport(page);
        return box.width >= live.width - 1 && box.height >= live.height - 1;
      };
      // The Fullscreen API, or the page's fallback class where an engine refuses the API.
      const inFullscreen = () =>
        page.evaluate(
          () =>
            Boolean(document.fullscreenElement) ||
            document
              .querySelector(".viewer")
              .classList.contains("is-fullscreen"),
        );
      // Preview pixel under a client point, read from the rendered canvas box.
      const pixelUnder = (x, y) =>
        page.locator("#canvas").evaluate(
          (element, [px, py]) => {
            const box = element.getBoundingClientRect();
            return [
              ((px - box.left) * element.width) / box.width,
              ((py - box.top) * element.height) / box.height,
            ];
          },
          [x, y],
        );
      const toolPressed = (name) =>
        page
          .getByRole("button", { name, exact: true })
          .getAttribute("aria-pressed");
      async function wheel(modifier, dx, dy) {
        await page.keyboard.down(modifier);
        await page.mouse.wheel(dx, dy);
        await page.keyboard.up(modifier);
      }
      async function dragBy(from, dx, dy) {
        await page.mouse.move(from[0], from[1]);
        await page.mouse.down();
        await page.mouse.move(from[0] + dx, from[1] + dy, { steps: 6 });
        await page.mouse.up();
      }
      async function zoomToAtLeast(width, attempts = 16) {
        if (attempts === 0 || (await canvasBox()).width >= width) return;
        await wheel("Control", 0, -200);
        await zoomToAtLeast(width, attempts - 1);
      }
      async function leaveFullscreen(attempts = 2) {
        if (attempts === 0 || !(await fillsViewport())) return;
        await page.keyboard.press("Escape");
        await leaveFullscreen(attempts - 1);
      }
      async function resetView() {
        await leaveFullscreen();
        const view = page.getByRole("button", { name: "View", exact: true });
        if (await view.count()) await view.click();
        const fit = page.getByRole("button", { name: "Fit", exact: true });
        if (await fit.count()) await fit.click();
      }
      async function check(name, body) {
        try {
          await body();
          checks.push(`PASS ${name}`);
        } catch (error) {
          checks.push(`FAIL ${name}: ${error.message.split("\n")[0]}`);
        }
      }

      // Runs first: the page has had no tool selected yet.
      await check(
        "plain click in the default mode adds no markup and stays windowed",
        async () => {
          const box = await canvasBox();
          await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
          await page.waitForTimeout(400);
          assert.equal(
            await fillsViewport(),
            false,
            "click entered fullscreen",
          );
          assert.equal(await inFullscreen(), false, "click entered fullscreen");
          assert.equal(markupSaves.length, 0, "click saved markup");
          assert.equal(
            await page.locator("#undo").isDisabled(),
            true,
            "click added markup",
          );
        },
      );

      await check("Ctrl+wheel zooms about the cursor", async () => {
        await resetView();
        const before = await canvasBox();
        const cursor = [
          before.x + before.width * 0.3,
          before.y + before.height * 0.4,
        ];
        await page.mouse.move(...cursor);
        const anchor = await pixelUnder(...cursor);
        await wheel("Control", 0, -400);
        await until(
          async () => (await canvasBox()).width > before.width * 1.5,
          "Ctrl+wheel zoom in",
          2000,
        );
        const after = await pixelUnder(...cursor);
        const drift = Math.hypot(after[0] - anchor[0], after[1] - anchor[1]);
        assert.ok(drift < 1, `pixel under cursor moved ${drift.toFixed(2)} px`);
        await wheel("Control", 0, 400);
        await until(
          async () => Math.abs((await canvasBox()).width - before.width) < 1,
          "Ctrl+wheel zoom back out",
          2000,
        );
        // While an image loads the canvas is hidden; Ctrl+wheel must still
        // not reach the engine's page zoom.
        const prevented = await page.evaluate(() => {
          const canvas = document.querySelector("#canvas");
          canvas.hidden = true;
          const wheelEvent = new WheelEvent("wheel", {
            ctrlKey: true,
            deltaY: -100,
            bubbles: true,
            cancelable: true,
          });
          document.querySelector("#canvas-stage").dispatchEvent(wheelEvent);
          canvas.hidden = false;
          return wheelEvent.defaultPrevented;
        });
        assert.equal(prevented, true, "Ctrl+wheel over a loading image not prevented");
      });

      // Engines differ in which axis carries a Shift+wheel delta; either one pans sideways.
      await check(
        "Shift+wheel pans horizontally without changing scale",
        async () => {
          await resetView();
          const before = await canvasBox();
          await page.mouse.move(
            before.x + before.width / 2,
            before.y + before.height / 2,
          );
          await wheel("Shift", 0, 120);
          await until(
            async () => (await canvasBox()).x < before.x - 20,
            "Shift+vertical wheel moves the image left",
            2000,
          );
          const first = await canvasBox();
          assert.ok(
            Math.abs(first.y - before.y) < 0.5,
            "Shift+vertical wheel moved y",
          );
          assert.ok(
            Math.abs(first.width - before.width) < 0.5,
            "pan changed scale",
          );
          await wheel("Shift", 120, 0);
          await until(
            async () => (await canvasBox()).x < first.x - 20,
            "Shift+horizontal wheel moves the image left",
            2000,
          );
          const second = await canvasBox();
          assert.ok(
            Math.abs(second.y - first.y) < 0.5,
            "Shift+horizontal wheel moved y",
          );
          assert.ok(
            Math.abs(second.width - before.width) < 0.5,
            "pan changed scale",
          );
        },
      );

      await check(
        "drag in View pans by the drag distance and adds no markup",
        async () => {
          await resetView();
          await page.getByRole("button", { name: "View", exact: true }).click();
          const saves = markupSaves.length;
          const before = await canvasBox();
          await dragBy(
            [before.x + before.width / 2, before.y + before.height / 2],
            60,
            40,
          );
          const after = await canvasBox();
          assert.ok(
            Math.abs(after.x - before.x - 60) < 1,
            `x moved ${after.x - before.x}`,
          );
          assert.ok(
            Math.abs(after.y - before.y - 40) < 1,
            `y moved ${after.y - before.y}`,
          );
          assert.ok(
            Math.abs(after.width - before.width) < 0.5,
            "drag changed scale",
          );
          await page.waitForTimeout(300);
          assert.equal(markupSaves.length, saves, "View drag saved markup");
        },
      );

      await check(
        "Fullscreen button enters fullscreen, a click keeps it, Esc leaves",
        async () => {
          await resetView();
          await page
            .getByRole("button", { name: "Fullscreen", exact: true })
            .click();
          await until(fillsViewport, "viewer fills the viewport", 3000);
          assert.equal(await inFullscreen(), true, "fullscreen not entered");
          const box = await canvasBox();
          await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
          await page.waitForTimeout(300);
          assert.equal(
            await fillsViewport(),
            true,
            "plain click left fullscreen",
          );
          await page.keyboard.press("Escape");
          await until(
            async () => !(await fillsViewport()),
            "Esc leaves fullscreen",
            3000,
          );
          assert.equal(await inFullscreen(), false, "fullscreen still active");
        },
      );

      await check(
        "Esc leaves fullscreen first, then returns a markup tool to View",
        async () => {
          await resetView();
          await page.getByRole("button", { name: "Pen", exact: true }).click();
          await page
            .getByRole("button", { name: "Fullscreen", exact: true })
            .click();
          await until(fillsViewport, "viewer fills the viewport", 3000);
          await page.keyboard.press("Escape");
          await until(
            async () => !(await fillsViewport()),
            "Esc leaves fullscreen",
            3000,
          );
          assert.equal(
            await toolPressed("Pen"),
            "true",
            "first Esc dropped the tool",
          );
          await page.keyboard.press("Escape");
          assert.equal(
            await toolPressed("View"),
            "true",
            "second Esc did not select View",
          );
          assert.equal(await toolPressed("Pen"), "false");
        },
      );

      await check(
        "markup keeps its image coordinates at 4x zoom, pan and fullscreen",
        async () => {
          await resetView();
          const fitBox = await canvasBox();
          await draw(page, "Rectangle", [40, 40], [120, 100]);
          await until(
            async () =>
              (await verdict(viewerResults)).images[0].markup.length > 0,
            "rectangle saved",
          );
          const stored = (await verdict(viewerResults)).images[0].markup;
          const rect = stored.at(-1);
          // Firefox reports pointer positions in whole CSS pixels.
          const cssPixel = 320 / fitBox.width;
          for (const [actual, expected] of [
            [rect.x, 40],
            [rect.y, 40],
            [rect.x + rect.w, 120],
            [rect.y + rect.h, 100],
          ])
            assert.ok(
              Math.abs(actual - expected) <= cssPixel,
              `rectangle corner ${actual} != ${expected}`,
            );
          await page.getByRole("button", { name: "View", exact: true }).click();
          const fit = await canvasBox();
          await page.mouse.move(fit.x + fit.width / 2, fit.y + fit.height / 2);
          await zoomToAtLeast(fit.width * 4);
          assert.ok(
            (await canvasBox()).width >= fit.width * 4,
            "did not reach 4x",
          );
          await dragBy([fit.x + fit.width / 2, fit.y + fit.height / 2], 30, 20);
          await page
            .getByRole("button", { name: "Fullscreen", exact: true })
            .click();
          await until(fillsViewport, "viewer fills the viewport", 3000);
          assert.deepEqual(
            (await verdict(viewerResults)).images[0].markup,
            stored,
            "zoom, pan and fullscreen changed stored markup",
          );
          await page
            .getByRole("button", { name: "Arrow", exact: true })
            .click();
          const zoomed = await canvasBox();
          const client = ([x, y]) => [
            zoomed.x + (x / 320) * zoomed.width,
            zoomed.y + (y / 240) * zoomed.height,
          ];
          const from = client([150, 110]);
          const to = client([170, 125]);
          const live = await liveViewport(page);
          for (const [x, y] of [from, to])
            assert.ok(
              x > 0 && y > 0 && x < live.width && y < live.height,
              "arrow off-screen",
            );
          await page.mouse.move(...from);
          await page.mouse.down();
          await page.mouse.move(...to, { steps: 6 });
          await page.mouse.up();
          await until(
            async () =>
              (await verdict(viewerResults)).images[0].markup.length ===
              stored.length + 1,
            "zoomed arrow saved",
          );
          const after = (await verdict(viewerResults)).images[0].markup;
          assert.deepEqual(
            after.slice(0, stored.length),
            stored,
            "earlier marks moved",
          );
          const arrow = after.at(-1);
          for (const [actual, expected] of [
            [arrow.x1, 150],
            [arrow.y1, 110],
            [arrow.x2, 170],
            [arrow.y2, 125],
          ])
            assert.ok(
              Math.abs(actual - expected) < 0.5,
              `arrow ${actual} != ${expected}`,
            );
          assert.equal(arrow.width, rect.width, "4x zoom changed the stroke width");
          await page.keyboard.press("Escape");
          await until(async () => !(await fillsViewport()), "Esc leaves fullscreen", 3000);
          await page.keyboard.press("Escape");
          await page.keyboard.press(" ");
          await page.getByRole("button", { name: "Text", exact: true }).click();
          const placeLabel = async (text, [x, y]) => {
            const box = await canvasBox();
            await page.mouse.click(
              box.x + (x / 320) * box.width,
              box.y + (y / 240) * box.height,
            );
            await page.locator("#text-input").fill(text);
            await page.locator("#text-input").press("Enter");
            await until(
              async () =>
                (await verdict(viewerResults)).images[0].markup.at(-1)?.text === text,
              `label ${text} saved`,
            );
            return (await verdict(viewerResults)).images[0].markup.at(-1);
          };
          const atFit = await placeLabel("Fit", [40, 200]);
          const centre = await canvasBox();
          await page.mouse.move(centre.x + centre.width / 2, centre.y + centre.height / 2);
          await zoomToAtLeast(centre.width * 4);
          const atZoom = await placeLabel("Zoom", [160, 120]);
          assert.ok(
            Math.abs(atZoom.size - atFit.size) < 0.01,
            `4x zoom changed the label size (${atFit.size} -> ${atZoom.size})`,
          );
          await page.keyboard.press("1");
        },
      );

      await check(
        "an image with alpha 0 on every pixel shows a checkerboard and a 100% badge",
        async () => {
          await resetView();
          await page.getByRole("button", { name: "Next", exact: true }).click();
          await until(
            async () =>
              page
                .locator("#canvas")
                .evaluate((element) => element.width === 1920),
            "alpha-zero image loaded",
          );
          await page
            .getByText("alpha 0 on 100% of pixels")
            .waitFor({ state: "visible", timeout: 3000 });
          const box = await canvasBox();
          const luma = await screenshotLuma(page, {
            x: Math.round(box.x + box.width / 2 - 32),
            y: Math.round(box.y + box.height / 2 - 32),
            width: 64,
            height: 64,
          });
          const light = Math.max(...luma);
          const dark = Math.min(...luma);
          assert.ok(
            dark > 60,
            `transparent slot is dark (min luma ${dark.toFixed(0)})`,
          );
          assert.ok(
            light - dark > 25,
            `no checkerboard contrast (${dark.toFixed(0)}-${light.toFixed(0)})`,
          );
        },
      );

      await check("a quarter-transparent image reports 25%", async () => {
        await resetView();
        await page.getByRole("button", { name: "Next", exact: true }).click();
        await until(
          async () =>
            page
              .locator("#canvas")
              .evaluate((element) => element.width === 320),
          "quarter-alpha image loaded",
        );
        await page
          .getByText("alpha 0 on 25% of pixels")
          .waitFor({ state: "visible", timeout: 3000 });
      });

      await check("control: an opaque image shows no alpha badge", async () => {
        await resetView();
        await page.locator("#index").fill("1");
        await page.locator("#index").press("Enter");
        await until(
          async () => (await page.locator("#index").inputValue()) === "1",
          "back to image 1",
        );
        await until(
          async () =>
            page
              .locator("#canvas")
              .evaluate((element) => element.width === 320),
          "opaque image loaded",
        );
        await page.waitForTimeout(300);
        assert.equal(
          await page.getByText(/alpha 0 on/).isVisible(),
          false,
          "opaque image shows a badge",
        );
      });

      // Simulates an engine that drops a canvas's pixels when the window
      // changes, which headless engines do not do: clear them, then resize.
      await check("a lost 2D canvas context is left for the engine to restore", async () => {
        // On a 2D canvas, cancelling contextlost stops the restore (unlike WebGL).
        const cancelled = await page.locator("#canvas").evaluate((element) => {
          const event = new Event("contextlost", { cancelable: true });
          element.dispatchEvent(event);
          return event.defaultPrevented;
        });
        assert.equal(cancelled, false, "contextlost was cancelled, so no restore");
      });

      await check("a window resize repaints a canvas that lost its pixels", async () => {
        await resetView();
        const centre = () =>
          page.locator("#canvas").evaluate((element) =>
            Array.from(
              element
                .getContext("2d")
                .getImageData(element.width / 2, element.height / 2, 1, 1).data,
            ),
          );
        const painted = await centre();
        assert.equal(painted[3], 255, "canvas not painted before the resize");
        await page.locator("#canvas").evaluate((element) =>
          element.getContext("2d").clearRect(0, 0, element.width, element.height),
        );
        assert.deepEqual(await centre(), [0, 0, 0, 0]);
        await page.setViewportSize({ width: 1100, height: 800 });
        await until(
          async () => (await centre())[3] === 255,
          "canvas repainted after the resize",
          3000,
        );
        await page.setViewportSize(viewport);
      });

      const indexIs = (value) =>
        until(
          async () => (await page.locator("#index").inputValue()) === value,
          `image ${value} selected`,
          2000,
        );
      const notesFocused = () =>
        page.evaluate(() => document.activeElement?.id === "notes");

      await check("an image click or Esc releases the notes box, then arrows navigate", async () => {
        await resetView();
        await indexIs("1");
        await page.locator("#notes").click();
        await page.keyboard.type("Arrow keys must reach the carousel.");
        const box = await canvasBox();
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        assert.equal(await notesFocused(), false, "image click kept focus in notes");
        await page.keyboard.press("ArrowRight");
        await indexIs("2");
        await page.locator("#notes").click();
        await page.keyboard.press("Escape");
        assert.equal(await notesFocused(), false, "Esc kept focus in notes");
        await page.keyboard.press("ArrowLeft");
        await indexIs("1");
        assert.equal(
          await page.locator("#notes").inputValue(),
          "Arrow keys must reach the carousel.",
        );
      });

      await check("keys 1-5 pick View..Text and Alt+1-4 pick the color presets", async () => {
        await resetView();
        await inSequence([
          ["2", "Pen"],
          ["3", "Arrow"],
          ["4", "Rectangle"],
          ["5", "Text"],
          ["1", "View"],
        ], async ([key, name]) => {
          await page.keyboard.press(key);
          assert.equal(await toolPressed(name), "true", `${key} did not pick ${name}`);
        });
        await inSequence([
          ["Alt+2", "#43d9bd"],
          ["Alt+4", "#68a9ff"],
          ["Alt+1", "#ff5c54"],
        ], async ([key, color]) => {
          await page.keyboard.press(key);
          assert.equal(await page.locator("#color").inputValue(), color, `${key} color`);
        });
      });

      await check("Space fits without pressing the focused button; Ctrl+F toggles fullscreen", async () => {
        await resetView();
        await page.getByRole("button", { name: "Fullscreen", exact: true }).click();
        await until(fillsViewport, "viewer fills the viewport", 3000);
        const fit = await canvasBox();
        await page.mouse.move(fit.x + fit.width / 2, fit.y + fit.height / 2);
        await wheel("Control", 0, -400);
        await until(async () => (await canvasBox()).width > fit.width * 1.5, "zoomed", 2000);
        const scrollY = await page.evaluate(() => scrollY);
        await page.keyboard.press(" ");
        await until(
          async () => Math.abs((await canvasBox()).width - fit.width) < 1,
          "Space restores Fit",
          2000,
        );
        await page.waitForTimeout(300);
        assert.equal(await fillsViewport(), true, "Space pressed the focused Fullscreen button");
        assert.equal(await page.evaluate(() => scrollY), scrollY, "Space scrolled the page");
        await page.keyboard.press("Control+f");
        await until(async () => !(await fillsViewport()), "Ctrl+F leaves fullscreen", 3000);
        await page.keyboard.press("Control+f");
        await until(fillsViewport, "Ctrl+F enters fullscreen", 3000);
        await page.keyboard.press("Escape");
        await until(async () => !(await fillsViewport()), "Esc leaves fullscreen", 3000);
      });

      await check("Ctrl+Z undoes, Ctrl+Y redoes, Ctrl+E clears; Ctrl+Z in notes stays text undo", async () => {
        await resetView();
        await indexIs("1");
        const durable = async () => (await verdict(viewerResults)).images[0].markup;
        const stored = await durable();
        assert.ok(stored.length >= 2, `need saved marks, have ${stored.length}`);
        const box = await canvasBox();
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        await page.keyboard.press("Control+z");
        await until(async () => (await durable()).length === stored.length - 1, "Ctrl+Z undo saved");
        await page.keyboard.press("Control+y");
        await until(
          async () => JSON.stringify(await durable()) === JSON.stringify(stored),
          "Ctrl+Y restores the undone mark",
        );
        await page.locator("#notes").click();
        await page.keyboard.type(" Typed.");
        await page.keyboard.press("Control+z");
        await page.waitForTimeout(500);
        assert.deepEqual(await durable(), stored, "Ctrl+Z in notes undid markup");
        await page.keyboard.press("Escape");
        await page.keyboard.press("Control+e");
        await until(async () => (await durable()).length === 0, "Ctrl+E cleared markup");
        assert.equal(await page.locator("#redo").isDisabled(), true, "Redo survives Clear");
      });

      await check("Esc during a drag cancels the shape and returns to View", async () => {
        await resetView();
        await page.waitForTimeout(400);
        const before = (await verdict(viewerResults)).images[0].markup;
        await page.keyboard.press("4");
        const box = await canvasBox();
        await page.mouse.move(box.x + box.width * 0.2, box.y + box.height * 0.2);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.6, { steps: 6 });
        await page.keyboard.press("Escape");
        await page.mouse.up();
        await page.waitForTimeout(600);
        assert.deepEqual(
          (await verdict(viewerResults)).images[0].markup,
          before,
          "Esc saved the in-progress rectangle",
        );
        assert.equal(await toolPressed("View"), "true", "Esc did not return to View");
      });

      await check("an error stays visible in fullscreen", async () => {
        await resetView();
        await page.keyboard.press("Control+f");
        await until(fillsViewport, "viewer fills the viewport", 3000);
        await page.keyboard.press("5");
        const box = await canvasBox();
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        await page.locator("#text-input").fill("x".repeat(241));
        const alert = page.locator("#viewer-alert");
        await alert.waitFor({ state: "visible", timeout: 2000 });
        assert.match(await alert.innerText(), /240/);
        const alertBox = await alert.boundingBox();
        const live = await liveViewport(page);
        assert.ok(
          alertBox.y >= 0 && alertBox.y < live.height,
          "alert off-screen",
        );
        await page.locator("#text-input").press("Escape");
        await page.keyboard.press("Escape");
        await until(async () => !(await fillsViewport()), "Esc leaves fullscreen", 3000);
        await page.keyboard.press("1");
      });

      await check("Esc during a pen stroke leaves an accurate save status", async () => {
        await resetView();
        await page.keyboard.press("2");
        const box = await canvasBox();
        await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.7);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.75, { steps: 6 });
        await page.keyboard.press("Escape");
        await page.mouse.up();
        await page.waitForTimeout(600);
        assert.doesNotMatch(
          await page.locator("#save-status-text").innerText(),
          /Stroke in progress/,
          "save status still claims a stroke in progress",
        );
        await page.keyboard.press("1");
      });

      await check("an error in fullscreen does not clip the image at Fit", async () => {
        await resetView();
        await page.keyboard.press("Control+f");
        await until(fillsViewport, "viewer fills the viewport", 3000);
        await page.keyboard.press("5");
        const box = await canvasBox();
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        await page.locator("#text-input").fill("y".repeat(241));
        await page.locator("#text-input").press("Escape");
        await page.keyboard.press(" ");
        await page.waitForTimeout(300);
        const image = await canvasBox();
        const stage = await page.locator("#canvas-stage").boundingBox();
        const clipped = [
          stage.y - image.y,
          image.y + image.height - (stage.y + stage.height),
        ].map((value) => Math.max(0, value));
        assert.ok(
          clipped.every((value) => value < 0.5),
          `image clipped by ${clipped.map((value) => value.toFixed(1)).join("/")} px (top/bottom)`,
        );
        await page.keyboard.press("Escape");
        await until(async () => !(await fillsViewport()), "Esc leaves fullscreen", 3000);
        await page.keyboard.press("1");
      });

      // Ends the session, so it runs last.
      await check("Ctrl+Enter in the text-label box places the label and finishes", async () => {
        await resetView();
        await page.keyboard.press("5");
        const box = await canvasBox();
        await page.mouse.click(box.x + box.width / 3, box.y + box.height / 3);
        await page.locator("#text-input").fill("Done label");
        await page.locator("#text-input").press("Control+Enter");
        await until(
          async () => (await verdict(viewerResults)).status === "done",
          "review marked done",
        );
        assert.equal(
          (await verdict(viewerResults)).images[0].markup.at(-1)?.text,
          "Done label",
          "Done did not place the open label",
        );
      });
    } finally {
      await closeViaApi(opened);
      await page.close().catch(() => {});
    }
    await until(() => serverStopped(opened), "viewer session cleanup");
    for (const line of checks) console.log(`viewer ${line}`);
    assert.deepEqual(
      checks.filter((line) => line.startsWith("FAIL")),
      [],
      "viewer checks failed",
    );
  }
  if (testFocus === "all" || testFocus === "video") {
    // Oracles: the video element's own state, rendered boxes, raw HTTP
    // responses and the durable review.json.
    const videoImages = join(root, "video-images");
    const videoResults = join(root, "video-results");
    await mkdir(videoImages);
    await mkdir(videoResults);
    await writeFile(join(videoImages, "a-still.png"), original);
    const clip = await readFile(join(tests, "fixtures", "loop-1s.webm"));
    await writeFile(join(videoImages, "b-loop.webm"), clip);
    // A real MP4 signature with an undecodable payload: every engine refuses it.
    await writeFile(
      join(videoImages, "c-broken.mp4"),
      Buffer.concat([
        Buffer.from("0000001866747970", "hex"),
        Buffer.from("isom0000isomiso2"),
        Buffer.alloc(4096, 0x5a),
      ]),
    );
    opened = JSON.parse(cli("open", [], videoResults, videoImages).stdout);
    await leakControl(opened, true);
    page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.on("pageerror", (error) => errors.push(error.message));
    const videoChecks = [];
    const videoCheck = async (name, body) => {
      try {
        await body();
        videoChecks.push(`PASS ${name}`);
      } catch (error) {
        videoChecks.push(`FAIL ${name}: ${error.message.split("\n")[0]}`);
      }
    };
    // Emulate an engine that refuses unmuted autoplay, as Chromium can.
    await page.addInitScript(() => {
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function () {
        if (!this.muted)
          return Promise.reject(
            new DOMException("blocked", window.reviewTestPlayError || "NotAllowedError"),
          );
        return play.call(this);
      };
    });
    try {
      await page.goto(opened.url);
      await page.locator("#canvas").waitFor({ state: "visible" });
      const video = page.locator("#video");
      const fillsViewport = async () => {
        const box = await page.locator(".viewer").boundingBox();
        const live = await liveViewport(page);
        return box.width >= live.width - 1 && box.height >= live.height - 1;
      };
      const indexIs = (value) =>
        until(
          async () => (await page.locator("#index").inputValue()) === value,
          `item ${value} selected`,
          3000,
        );
      // Paths under the private session URL (apiUrl adds an api/ prefix).
      const sessionUrl = (path) =>
        new URL(path, opened.url.endsWith("/") ? opened.url : `${opened.url}/`);
      const postJson = (path, body) =>
        fetch(sessionUrl(path), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });

      await videoCheck("a video is one carousel item", async () => {
        assert.equal(await page.locator("#index-total").innerText(), "/ 3");
        await page.keyboard.press("ArrowRight");
        await indexIs("2");
        await video.waitFor({ state: "visible", timeout: 5000 });
        assert.equal(await page.locator("#canvas").isVisible(), false);
        await until(
          async () =>
            video.evaluate((element) => element.muted && !element.paused),
          "refused unmuted autoplay falls back to muted playback",
          5000,
        );
      });

      await videoCheck(
        "the video plays, loops, and has play/pause and a scrub bar",
        async () => {
          assert.equal(await video.evaluate((element) => element.controls), true);
          await video.evaluate((element) => {
            element.muted = true;
            return element.play();
          });
          await until(
            async () =>
              (await video.evaluate((element) => element.currentTime)) > 0.2,
            "video advances",
            5000,
          );
          let previous = 0;
          let wrapped = false;
          await until(
            async () => {
              const now = await video.evaluate((element) => element.currentTime);
              if (now + 0.05 < previous) wrapped = true;
              previous = now;
              return wrapped;
            },
            "video wraps to the start",
            6000,
          );
          assert.equal(await video.evaluate((element) => element.ended), false);
        },
      );

      await videoCheck("looping makes about one media request per loop", async () => {
        const mediaRequests = [];
        const onRequest = (request) => {
          if (new URL(request.url()).pathname.endsWith("/media/1"))
            mediaRequests.push(request.url());
        };
        page.on("request", onRequest);
        await video.evaluate((element) => {
          element.muted = true;
          element.currentTime = 0;
          return element.play();
        });
        await page.waitForTimeout(3500);
        page.off("request", onRequest);
        assert.ok(
          mediaRequests.length <= 6,
          `${mediaRequests.length} media requests over about 3 loops`,
        );
      });

      await videoCheck("markup, zoom and pan are disabled on the video", async () => {
        await inSequence(
          [
            "View",
            "Pen",
            "Arrow",
            "Rectangle",
            "Text",
            "Fit",
            "Undo",
            "Redo",
            "Clear",
          ],
          async (name) =>
            assert.equal(
              await page.getByRole("button", { name, exact: true }).isDisabled(),
              true,
              `${name} enabled on a video`,
            ),
        );
        const videoBox = async () => {
          await page.waitForTimeout(150);
          const box = await video.boundingBox();
          return [box.x, box.y, box.width, box.height].map(Math.round);
        };
        const start = await video.boundingBox();
        const before = await videoBox();
        await page.mouse.move(
          start.x + start.width / 2,
          start.y + start.height / 3,
        );
        await page.keyboard.down("Control");
        await page.mouse.wheel(0, -400);
        await page.keyboard.up("Control");
        const afterZoom = await videoBox();
        await page.keyboard.down("Shift");
        await page.mouse.wheel(0, 120);
        await page.keyboard.up("Shift");
        const afterPan = await videoBox();
        await page.keyboard.press("2");
        const after = await videoBox();
        assert.deepEqual(
          after,
          before,
          `the video moved or scaled: before ${before}, after Ctrl+wheel ` +
            `${afterZoom}, after Shift+wheel ${afterPan}, after the 2 key ${after}`,
        );
        assert.equal(
          await page
            .getByRole("button", { name: "Pen", exact: true })
            .getAttribute("aria-pressed"),
          "false",
          "key 2 picked Pen on a video",
        );
      });

      await videoCheck("Fullscreen and Esc work on the video", async () => {
        const windowed = await video.boundingBox();
        await page.getByRole("button", { name: "Fullscreen", exact: true }).click();
        await until(fillsViewport, "viewer fills the viewport", 3000);
        assert.ok(
          (await video.boundingBox()).height > windowed.height,
          "video did not grow",
        );
        await page.keyboard.press("Escape");
        await until(
          async () => !(await fillsViewport()),
          "Esc leaves fullscreen",
          3000,
        );
      });

      await videoCheck("the server streams the video with byte ranges", async () => {
        const url = sessionUrl("media/1");
        const part = await fetch(url, { headers: { Range: "bytes=10-109" } });
        assert.equal(part.status, 206);
        assert.equal(
          part.headers.get("content-range"),
          `bytes 10-109/${clip.length}`,
        );
        assert.deepEqual(
          Buffer.from(await part.arrayBuffer()),
          clip.subarray(10, 110),
        );
        const whole = await fetch(url);
        assert.equal(whole.status, 200);
        assert.equal(whole.headers.get("content-type"), "video/webm");
        assert.equal(whole.headers.get("accept-ranges"), "bytes");
        assert.deepEqual(Buffer.from(await whole.arrayBuffer()), clip);
        assert.equal((await fetch(sessionUrl("image/1"))).status, 404);
      });

      await videoCheck("the server refuses markup on a video item", async () => {
        const state = await (await fetch(sessionUrl("api/state"))).json();
        assert.equal(state.images[1].kind, "video");
        assert.equal((await postJson("api/prepare", { index: 1 })).status, 400);
        const revision = state.images[1].revision + 1;
        const save = await postJson("api/save", {
          index: 1,
          revision,
          notes_delta: [],
          markup_delta: [
            {
              revision,
              op: "append",
              mark: {
                type: "rect",
                color: "#ff0000",
                width: 2,
                x: 1,
                y: 1,
                w: 5,
                h: 5,
              },
            },
          ],
        });
        assert.equal(save.status, 400);
        assert.deepEqual((await verdict(videoResults)).images[1].markup, []);
      });

      await videoCheck("a note on the video round-trips through review.json", async () => {
        await page.locator("#notes").fill("Loop seam at 0:01 is clean.");
        await until(async () => {
          const saved = (await verdict(videoResults)).images[1];
          return (
            saved.kind === "video" && saved.notes === "Loop seam at 0:01 is clean."
          );
        }, "video note saved");
        await page.keyboard.press("Escape");
      });

      await videoCheck("typing a note saves when typing pauses, not per keystroke", async () => {
        const saves = [];
        const onSave = (request) => {
          if (new URL(request.url()).pathname.endsWith("/api/save"))
            saves.push(request.url());
        };
        page.on("request", onSave);
        await page.locator("#notes").click();
        await page.keyboard.press("End");
        await page.keyboard.type(" Seam holds.", { delay: 220 });
        await page.waitForTimeout(1600);
        page.off("request", onSave);
        assert.ok(saves.length <= 2, `${saves.length} saves while typing 12 characters`);
        await until(
          async () =>
            (await verdict(videoResults)).images[1].notes ===
            "Loop seam at 0:01 is clean. Seam holds.",
          "typed note saved after the pause",
        );
        await page.keyboard.press("Escape");
      });

      await videoCheck("an undecodable video says this browser cannot play it", async () => {
        await page.keyboard.press("ArrowRight");
        await indexIs("3");
        const message = page.locator("#empty-state");
        await until(
          async () =>
            (await message.innerText()).includes(
              "This browser cannot play this file. Try an H.264 MP4 or a WebM file.",
            ),
          "playback error shown",
          30000,
        );
        assert.equal(await video.isVisible(), false, "the broken video stays hidden");
      });

      await videoCheck("markup tools come back on the image", async () => {
        await page.locator("#index").fill("1");
        await page.locator("#index").press("Enter");
        await indexIs("1");
        await page.locator("#canvas").waitFor({ state: "visible" });
        assert.equal(await video.isVisible(), false);
        const pen = page.getByRole("button", { name: "Pen", exact: true });
        assert.equal(await pen.isDisabled(), false);
        await page.keyboard.press("2");
        assert.equal(await pen.getAttribute("aria-pressed"), "true");
        await page.keyboard.press("1");
      });

      await videoCheck("the markup tool comes back after a video", async () => {
        await page.keyboard.press("2");
        await page.keyboard.press("ArrowRight");
        await indexIs("2");
        const pen = page.getByRole("button", { name: "Pen", exact: true });
        assert.equal(await pen.isDisabled(), true);
        await page.keyboard.press("ArrowLeft");
        await indexIs("1");
        await page.locator("#canvas").waitFor({ state: "visible" });
        assert.equal(await pen.getAttribute("aria-pressed"), "true", "Pen not restored");
        await page.keyboard.press("1");
      });

      await videoCheck("only a refused autoplay falls back to muted playback", async () => {
        await video.evaluate((element) => {
          element.muted = false;
          window.reviewTestPlayError = "AbortError";
        });
        await page.keyboard.press("ArrowRight");
        await indexIs("2");
        await page.waitForTimeout(800);
        assert.equal(
          await video.evaluate((element) => element.muted),
          false,
          "an aborted play() muted the video",
        );
        await page.evaluate(() => {
          window.reviewTestPlayError = "NotAllowedError";
        });
        await page.keyboard.press("ArrowLeft");
        await indexIs("1");
      });

      // Ends the session, so it runs last.
      await videoCheck("Done keeps the video note and stops the video", async () => {
        await page.keyboard.press("ArrowRight");
        await indexIs("2");
        await until(
          async () => video.evaluate((element) => !element.paused),
          "video playing before Done",
          5000,
        );
        await page.locator("#notes").click();
        await page.keyboard.press("Control+Enter");
        await until(
          async () => (await verdict(videoResults)).status === "done",
          "review done",
        );
        assert.equal(
          (await verdict(videoResults)).images[1].notes,
          "Loop seam at 0:01 is clean. Seam holds.",
        );
        // The server records done before the page handles its response.
        await until(
          async () => video.evaluate((element) => element.paused),
          "video stops after Done",
          3000,
        );
      });

      await videoCheck("the video frame stays on screen after Done", async () => {
        assert.equal(await video.isVisible(), true, "Done blanked the video");
      });

      await videoCheck("a late loadedmetadata after Done does not restart the video", async () => {
        await video.evaluate((element) =>
          element.dispatchEvent(new Event("loadedmetadata")),
        );
        await page.waitForTimeout(300);
        assert.equal(
          await video.evaluate((element) => element.paused),
          true,
          "a late loadedmetadata restarted the video after Done",
        );
      });

      await videoCheck("Done mid-load still reveals the video when its metadata arrives", async () => {
        // Simulate Done before the clip's metadata loaded: hidden behind the loading panel.
        await video.evaluate((element) => {
          element.hidden = true;
          document.querySelector("#empty-state").hidden = false;
          element.dispatchEvent(new Event("loadedmetadata"));
        });
        await page.waitForTimeout(300);
        assert.equal(await video.isVisible(), true, "the video stayed behind the loading panel");
        assert.equal(await page.locator("#empty-state").isVisible(), false);
      });

      await videoCheck("a late media error after Done keeps the frame", async () => {
        await video.evaluate((element) => element.dispatchEvent(new Event("error")));
        await page.waitForTimeout(300);
        assert.equal(await video.isVisible(), true, "a late error hid the video after Done");
      });
    } finally {
      await closeViaApi(opened);
      await page.close().catch(() => {});
    }
    await until(() => serverStopped(opened), "video session cleanup");
    for (const line of videoChecks) console.log(`video ${line}`);
    assert.deepEqual(
      videoChecks.filter((line) => line.startsWith("FAIL")),
      [],
      "video checks failed",
    );
  }
  if (testFocus === "all" || testFocus === "lifecycle") {
    // Oracles: the CLI status and wait result, review.json, and the request
    // bodies each page sends; never the page's own lifecycle variables.
    const lifecycleChecks = [];
    async function lifecycleCheck(name, body) {
      try {
        await body();
        lifecycleChecks.push(`PASS ${name}`);
      } catch (error) {
        lifecycleChecks.push(`FAIL ${name}: ${error.message.split("\n")[0]}`);
      }
    }
    const lifecyclePages = [];
    async function closeLifecyclePages() {
      await Promise.all(
        lifecyclePages
          .splice(0)
          .map((target) => target.close().catch(() => {})),
      );
    }
    // Loads the session in a new page and records, in order, its heartbeat
    // bodies and the page_id of each api/state request.
    async function lifecyclePage(session, initScript) {
      const target = await context.newPage();
      lifecyclePages.push(target);
      target.setDefaultTimeout(5000);
      target.on("pageerror", (error) => errors.push(error.message));
      const sent = { heartbeats: [], pageIds: [] };
      target.on("request", (request) => {
        const url = new URL(request.url());
        if (url.pathname.endsWith("/api/heartbeat"))
          sent.heartbeats.push(request.postDataJSON());
        else if (url.pathname.endsWith("/api/state"))
          sent.pageIds.push(url.searchParams.get("page_id"));
      });
      if (initScript) await target.addInitScript(initScript);
      await target.goto(session.url);
      await target.locator("#canvas").waitFor({ state: "visible" });
      await until(() => sent.heartbeats.length > 0, "first heartbeat");
      return { target, sent };
    }
    async function lifecycleSession(name) {
      const sessionResults = join(root, `lifecycle-${name}`);
      await mkdir(sessionResults);
      const session = JSON.parse(cli("open", [], sessionResults).stdout);
      await leakControl(session, true);
      return session;
    }
    const pageIdPattern = /^[0-9a-f]{8}(-?[0-9a-f]{4}){3}-?[0-9a-f]{12}$/;

    opened = await lifecycleSession("reload");
    let lifecycleEnded = false;
    let sent;
    try {
      ({ target: page, sent } = await lifecyclePage(opened));
      await lifecycleCheck(
        "a reload within the close grace keeps the review open under a new page_id",
        async () => {
          const loads = sent.pageIds.length;
          // Reload within the close grace keeps the review open.
          await page.reload();
          await page.waitForFunction(() =>
            document.title.startsWith("Visual review"),
          );
          await sleep(3000);
          assert.equal(
            readStatus(opened.results).status,
            "open",
            "reload ended the review",
          );
          const [before, after] = sent.pageIds.slice(loads - 1, loads + 1);
          assert.match(before ?? "", pageIdPattern, "api/state named no page");
          assert.match(after ?? "", pageIdPattern, "api/state named no page");
          assert.notEqual(after, before, "the reloaded page kept its page_id");
          assert.equal(
            sent.heartbeats.at(-1).page_id,
            after,
            "heartbeats do not name the reloaded page",
          );
        },
      );
      await lifecycleCheck(
        "a page restored from the back/forward cache loads the review again",
        async () => {
          const loads = sent.pageIds.length;
          await page.evaluate(() =>
            window.dispatchEvent(
              new PageTransitionEvent("pageshow", { persisted: true }),
            ),
          );
          await until(
            () => sent.pageIds.length > loads,
            "the restored page requests api/state",
            5000,
          );
          await page.locator("#canvas").waitFor({ state: "visible" });
          assert.notEqual(
            sent.pageIds.at(-1),
            sent.pageIds[loads - 1],
            "the restored page kept its page_id",
          );
          await sleep(3000);
          assert.equal(
            readStatus(opened.results).status,
            "open",
            "loading the review again ended it",
          );
        },
      );
      await lifecycleCheck(
        "a hidden tab reports itself hidden and pauses the close timer",
        async () => {
          const beats = sent.heartbeats.length;
          try {
            // A hidden tab pauses the close timer.
            await page.evaluate(() => {
              Object.defineProperty(document, "visibilityState", {
                value: "hidden",
                configurable: true,
              });
              document.dispatchEvent(new Event("visibilitychange"));
            });
            await sleep(5000); // longer than the 2 s close timeout used by this suite
            assert.equal(
              readStatus(opened.results).status,
              "open",
              "hidden tab was treated as closed",
            );
            // A tab that still reported itself visible would keep the review open by itself.
            const whileHidden = sent.heartbeats.slice(beats);
            const reported = whileHidden.findIndex(
              (beat) => beat.visible === false,
            );
            assert.ok(
              reported >= 0,
              "the hidden tab never reported itself hidden",
            );
            assert.ok(
              whileHidden
                .slice(reported)
                .every((beat) => beat.visible === false),
              "the hidden tab reported itself visible",
            );
          } finally {
            await page.evaluate(() => {
              Object.defineProperty(document, "visibilityState", {
                value: "visible",
                configurable: true,
              });
              document.dispatchEvent(new Event("visibilitychange"));
            });
          }
          const shown = sent.heartbeats.length;
          await until(
            () =>
              sent.heartbeats
                .slice(shown)
                .some((beat) => beat.visible === true),
            "the tab reports itself visible again",
            3000,
          );
        },
      );
      await lifecycleCheck(
        "Done shows the completion message and stops heartbeats",
        async () => {
          const doneWatch = watcher(opened.results);
          doneWatch.catch(() => {});
          // Done shows the completion message and stops heartbeats.
          await page.getByRole("button", { name: "Done" }).click();
          await page
            .getByText("Review complete. You can close this tab.")
            .waitFor();
          const beats = sent.heartbeats.length;
          await sleep(2000); // three heartbeat intervals at this suite's close timeout
          assert.equal(
            sent.heartbeats.length,
            beats,
            "heartbeats continued after Done",
          );
          assert.deepEqual(await doneWatch, {
            code: 0,
            stdout: "done",
            stderr: "",
          });
          lifecycleEnded = true;
        },
      );
    } finally {
      if (!lifecycleEnded) await closeViaApi(opened);
      await closeLifecyclePages();
    }
    await until(() => serverStopped(opened), "reload session cleanup");

    // visibilitychange never fires for a tab that is hidden from the start.
    opened = await lifecycleSession("hidden");
    lifecycleEnded = false;
    try {
      ({ target: page, sent } = await lifecyclePage(opened, () => {
        Object.defineProperty(document, "visibilityState", {
          value: "hidden",
          configurable: true,
        });
        delete Crypto.prototype.randomUUID;
      }));
      await lifecycleCheck(
        "a tab that loads hidden reports it at once and keeps the review open",
        async () => {
          assert.equal(
            sent.heartbeats[0].visible,
            false,
            "the first heartbeat of a hidden tab reported it visible",
          );
          await sleep(3500); // longer than the 2 s close timeout used by this suite
          assert.equal(
            readStatus(opened.results).status,
            "open",
            "a tab that loaded hidden was treated as closed",
          );
          assert.ok(
            sent.heartbeats.every((beat) => beat.visible === false),
            "the hidden tab reported itself visible",
          );
        },
      );
      await lifecycleCheck(
        "without crypto.randomUUID the page_id comes from crypto.getRandomValues",
        async () => {
          assert.match(sent.pageIds[0] ?? "", /^[0-9a-f]{32}$/);
          assert.ok(
            sent.heartbeats.every((beat) => beat.page_id === sent.pageIds[0]),
            "heartbeats do not name their page",
          );
        },
      );
      await lifecycleCheck(
        "leaving a hidden page ends the review after the close grace",
        async () => {
          // Hidden, the page has paused the close timer, so only its close
          // request can end the review before the watcher gives up. Leaving
          // fires pagehide as closing the tab does, but Firefox does not
          // reliably deliver a request that pagehide starts in a closing tab.
          const closeWatch = watcher(opened.results);
          closeWatch.catch(() => {});
          await page.goto("about:blank");
          assert.deepEqual(await closeWatch, {
            code: 10,
            stdout: "closed",
            stderr: "",
          });
          lifecycleEnded = true;
        },
      );
    } finally {
      if (!lifecycleEnded) await closeViaApi(opened);
      await closeLifecyclePages();
    }
    await until(() => serverStopped(opened), "hidden session cleanup");

    opened = await lifecycleSession("prompt");
    lifecycleEnded = false;
    try {
      // Browsers show a leave prompt only after a user gesture, so each page gets a click.
      await lifecycleCheck(
        "an idle page closes without a leave prompt",
        async () => {
          const { target: idle } = await lifecyclePage(opened);
          const dialogs = [];
          idle.on("dialog", (dialog) => {
            dialogs.push(dialog.type());
            dialog.accept().catch(() => {});
          });
          await idle.locator("#notes").click();
          await idle.locator("#notes").fill("Saved before leaving.");
          await until(
            async () =>
              (await verdict(opened.results)).images[0].notes ===
                "Saved before leaving." &&
              (await idle.locator("#save-status-text").innerText()) ===
                "All changes saved",
            "the note is saved and no save is pending",
          );
          const closed = idle.waitForEvent("close");
          await idle.close({ runBeforeUnload: true });
          await closed;
          assert.deepEqual(dialogs, [], "an idle page asked before leaving");
        },
      );
      await lifecycleCheck(
        "a save in flight asks before leaving, and staying keeps the review",
        async () => {
          // This page loads within the close grace that the idle page started.
          const { target: busy } = await lifecyclePage(opened);
          const dialogs = [];
          busy.on("dialog", (dialog) => {
            dialogs.push(dialog.type());
            dialog.dismiss().catch(() => {});
          });
          let heldSave;
          await busy.route("**/api/save", (route) => {
            heldSave = route;
          });
          await busy.locator("#notes").click();
          await busy.locator("#notes").fill("Typed while a save was pending.");
          await until(
            () => heldSave !== undefined,
            "the autosave is in flight",
          );
          await busy.close({ runBeforeUnload: true });
          await until(() => dialogs.length > 0, "the leave prompt", 3000);
          await sleep(500);
          assert.equal(busy.isClosed(), false, "the page closed after Stay");
          await heldSave.continue();
          await busy.unroute("**/api/save");
          await until(
            async () =>
              (await verdict(opened.results)).images[0].notes ===
                "Typed while a save was pending." &&
              (await busy.locator("#save-status-text").innerText()) ===
                "All changes saved",
            "the held save completes after Stay",
          );
          const closeWatch = watcher(opened.results);
          closeWatch.catch(() => {});
          const closed = busy.waitForEvent("close");
          await busy.close({ runBeforeUnload: true });
          await closed;
          assert.deepEqual(
            dialogs,
            ["beforeunload"],
            "the prompt must appear while the save is in flight, and only then",
          );
          assert.deepEqual(await closeWatch, {
            code: 10,
            stdout: "closed",
            stderr: "",
          });
          lifecycleEnded = true;
        },
      );
    } finally {
      if (!lifecycleEnded) await closeViaApi(opened);
      await closeLifecyclePages();
    }
    await until(() => serverStopped(opened), "prompt session cleanup");

    // Every save fails here, so only the pagehide beacon can deliver the note.
    // Leaving the page fires pagehide in every engine, and Firefox delivers
    // the requests it starts then, which it may not do when a tab closes.
    opened = await lifecycleSession("beacon");
    lifecycleEnded = false;
    try {
      await lifecycleCheck(
        "pagehide sends the edits that no save delivered",
        async () => {
          const note = "Only the close beacon carries this note.";
          const { target: leaving } = await lifecyclePage(opened);
          await leaving.route("**/api/save", (route) => route.abort());
          await leaving.locator("#notes").fill(note);
          await until(
            async () =>
              (await leaving.locator("#save-status-text").innerText()) ===
              "Save failed",
            "the autosave failed",
          );
          assert.equal(
            (await verdict(opened.results)).images[0].notes,
            "",
            "a save reached the server",
          );
          const closeWatch = watcher(opened.results);
          closeWatch.catch(() => {});
          await leaving.goto("about:blank");
          assert.deepEqual(await closeWatch, {
            code: 10,
            stdout: "closed",
            stderr: "",
          });
          lifecycleEnded = true;
          assert.equal(
            (await verdict(opened.results)).images[0].notes,
            note,
            "the close beacon lost the pending note",
          );
        },
      );
    } finally {
      if (!lifecycleEnded) await closeViaApi(opened);
      await closeLifecyclePages();
    }
    await until(() => serverStopped(opened), "beacon session cleanup");

    opened = await lifecycleSession("upload");
    lifecycleEnded = false;
    try {
      const { target: drawing } = await lifecyclePage(opened);
      const uploads = [];
      drawing.on("request", (request) => {
        const url = new URL(request.url());
        if (url.pathname.endsWith("/api/marked/0"))
          uploads.push(url.searchParams.get("revision"));
      });
      await lifecycleCheck(
        "an upload waits until the server acknowledges its markup",
        async () => {
          let heldSave;
          await drawing.route("**/api/save", (route) => {
            heldSave = route;
          });
          await draw(drawing, "Rectangle", [40, 40], [120, 100]);
          await until(() => heldSave !== undefined, "the markup save is held");
          await sleep(2500); // past the 1 s upload delay
          assert.deepEqual(uploads, [], "a PNG was uploaded before its markup was saved");
          await heldSave.continue();
          await drawing.unroute("**/api/save");
          const entry = await markedCurrent(opened.results, 0);
          await until(() => uploads.length > 0, "the upload request event");
          assert.deepEqual(
            uploads,
            [String(entry.markup_revision)],
            "one upload, at the acknowledged markup revision",
          );
        },
      );
      await lifecycleCheck(
        "an upload in flight asks before leaving, and staying lets it finish",
        async () => {
          const dialogs = [];
          drawing.on("dialog", (dialog) => {
            dialogs.push(dialog.type());
            dialog.dismiss().catch(() => {});
          });
          let heldUpload;
          await drawing.route("**/api/marked/0*", (route) => {
            heldUpload = route;
          });
          await draw(drawing, "Arrow", [40, 200], [200, 200]);
          await until(() => heldUpload !== undefined, "the upload is held");
          assert.equal(
            (await verdict(opened.results)).images[0].markup.length,
            2,
            "the arrow is saved, so only the upload is pending",
          );
          await drawing.close({ runBeforeUnload: true });
          await until(() => dialogs.length > 0, "the leave prompt", 3000);
          await sleep(500);
          assert.equal(drawing.isClosed(), false, "the page closed after Stay");
          await heldUpload.continue();
          await drawing.unroute("**/api/marked/0*");
          await markedCurrent(opened.results, 0);
          const closeWatch = watcher(opened.results);
          closeWatch.catch(() => {});
          const closed = drawing.waitForEvent("close");
          await drawing.close({ runBeforeUnload: true });
          await closed;
          assert.deepEqual(
            dialogs,
            ["beforeunload"],
            "the prompt must appear while the upload is in flight, and only then",
          );
          assert.deepEqual(await closeWatch, {
            code: 10,
            stdout: "closed",
            stderr: "",
          });
          lifecycleEnded = true;
        },
      );
    } finally {
      if (!lifecycleEnded) await closeViaApi(opened);
      await closeLifecyclePages();
    }
    await until(() => serverStopped(opened), "upload session cleanup");

    // Another writer (a second tab, or a reloaded page's late close beacon)
    // saves new markup while this page's first prepare is held. The page must
    // not adopt that markup revision, so it never uploads its older markup.
    const seedPen = {
      type: "pen",
      color: "#ef4444",
      width: 6,
      points: [
        [20, 30],
        [300, 30],
      ],
    };
    const foreignRect = {
      type: "rect",
      color: "#3b82f6",
      width: 6,
      x: 100,
      y: 120,
      w: 120,
      h: 80,
    };
    async function postApi(session, path, body) {
      const response = await fetch(apiUrl(session, path), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const answer = await response.json();
      assert.equal(response.status, 200, JSON.stringify(answer));
      return answer;
    }
    async function foreignMarkupCheck(name, description, notes, typeNote, savedAs) {
      opened = await lifecycleSession(name);
      try {
        await lifecycleCheck(description, async () => {
          await prepareOverHttp(opened, 0, 320, 240);
          await postApi(opened, "save", {
            index: 0,
            revision: 1,
            notes,
            markup: [seedPen],
          });
          const target = await context.newPage();
          lifecyclePages.push(target);
          target.setDefaultTimeout(5000);
          target.on("pageerror", (error) => errors.push(error.message));
          const uploads = [];
          target.on("request", (request) => {
            if (new URL(request.url()).pathname.includes("/api/marked/"))
              uploads.push(request.url());
          });
          let heldPrepare;
          await target.route("**/api/prepare", (route) => {
            heldPrepare = route;
          });
          await target.goto(opened.url);
          await until(() => heldPrepare !== undefined, "the page's prepare is held");
          await postApi(opened, "save", {
            index: 0,
            revision: 2,
            notes,
            markup: [seedPen, foreignRect],
          });
          const pageSave = target.waitForResponse((response) =>
            new URL(response.url()).pathname.endsWith("/api/save"),
          );
          await typeNote(target);
          savedAs(await (await pageSave).json());
          await heldPrepare.continue();
          await target
            .locator("#error-message")
            .getByText("reopen the review before adding markup")
            .waitFor();
          await sleep(2500); // past the 1 s upload delay
          assert.deepEqual(uploads, [], "the page uploaded its older markup");
          assert.equal(
            await target.locator("#canvas").isVisible(),
            false,
            "markup stayed on",
          );
          const entry = readStatus(opened.results).images[0];
          assert.deepEqual(
            [entry.markup.length, entry.png_stale, entry.marked],
            [2, true, null],
          );
        });
      } finally {
        await closeViaApi(opened);
        await closeLifecyclePages();
      }
      await until(() => serverStopped(opened), `${name} session cleanup`);
    }
    await foreignMarkupCheck(
      "foreign-prepare",
      "markup another page saved before prepare turns markup off and uploads nothing",
      "",
      (target) => target.locator("#notes").fill("Typed while the image loads."),
      (saved) =>
        assert.equal(saved.ignored, true, "precondition: the page's save collided"),
    );
    // Two keystrokes share one save. The server skips the patch at the
    // revision the other writer used and applies the next one, so the save
    // succeeds and reports the other writer's markup revision.
    await foreignMarkupCheck(
      "foreign-save",
      "a notes save that reports another page's markup revision is not adopted",
      "Seed note.",
      async (target) => {
        await target.locator("#notes").click();
        await target.keyboard.press("Home");
        await target.keyboard.type("ab");
      },
      (saved) =>
        assert.deepEqual(
          [saved.ignored, saved.revision, saved.markup_revision],
          [false, 3, 2],
          "precondition: the page's save applied after the other writer's",
        ),
    );

    opened = await lifecycleSession("export-failure");
    lifecycleEnded = false;
    try {
      await lifecycleCheck(
        "a PNG the browser cannot encode leaves it stale and does not block Done",
        async () => {
          // toBlob reports null when the browser cannot encode the canvas.
          const { target: failing } = await lifecyclePage(opened, () => {
            HTMLCanvasElement.prototype.toBlob = function (callback) {
              setTimeout(() => callback(null), 0);
            };
          });
          await draw(failing, "Rectangle", [40, 40], [120, 100]);
          await failing
            .locator("#error-message")
            .getByText(
              "This browser cannot render the marked PNG for this image. Your markup is saved.",
            )
            .waitFor({ state: "visible" });
          const doneWatch = watcher(opened.results);
          doneWatch.catch(() => {});
          await failing.getByRole("button", { name: "Done", exact: true }).click();
          assert.deepEqual(await doneWatch, {
            code: 0,
            stdout: "done",
            stderr: "",
          });
          lifecycleEnded = true;
          const entry = (await verdict(opened.results)).images[0];
          assert.deepEqual(
            [entry.markup.length, entry.png_stale, entry.marked],
            [1, true, null],
            "the markup is durable and the PNG stays stale",
          );
          assert.equal(existsSync(join(opened.results, "first.marked.png")), false);
        },
      );
    } finally {
      if (!lifecycleEnded) await closeViaApi(opened);
      await closeLifecyclePages();
    }
    await until(() => serverStopped(opened), "export-failure session cleanup");

    opened = await lifecycleSession("noise");
    try {
      await lifecycleCheck(
        "a browser that changes read-back canvas pixels gets a lasting warning",
        async () => {
          const warning =
            "This browser changes canvas pixels (privacy protection). Marked PNGs may contain noise.";
          const { target: plain } = await lifecyclePage(opened);
          assert.equal(
            await plain.getByText(warning).isVisible(),
            false,
            "a browser that reads pixels back unchanged was warned",
          );
          // Fingerprinting protection alters the pixels getImageData returns.
          const { target: noisy } = await lifecyclePage(opened, () => {
            const read = CanvasRenderingContext2D.prototype.getImageData;
            CanvasRenderingContext2D.prototype.getImageData = function (...args) {
              const pixels = read.apply(this, args);
              pixels.data[0] ^= 1;
              return pixels;
            };
          });
          await noisy.getByText(warning).waitFor({ state: "visible" });
          // An edit clears errors; the warning stays.
          await noisy.locator("#notes").fill("Edited after the warning.");
          await until(
            async () =>
              (await verdict(opened.results)).images[0].notes ===
              "Edited after the warning.",
            "the edit is saved",
          );
          assert.equal(await noisy.getByText(warning).isVisible(), true);
        },
      );
    } finally {
      await closeViaApi(opened);
      await closeLifecyclePages();
    }
    await until(() => serverStopped(opened), "noise session cleanup");
    for (const line of lifecycleChecks) console.log(`lifecycle ${line}`);
    assert.deepEqual(
      lifecycleChecks.filter((line) => line.startsWith("FAIL")),
      [],
      "lifecycle checks failed",
    );
  }
  assert.deepEqual(errors, [], "No browser errors");
  assert.deepEqual(
    await readFile(join(images, "first.png")),
    original,
    "Browser boundary tests leave original bytes unchanged",
  );
  assert.deepEqual(
    await readFile(join(images, "untouched.png")),
    original,
    "Browser boundary tests leave untouched original bytes unchanged",
  );
  assert.deepEqual(
    boundaryFailures,
    [],
    "Boundary failures: " + boundaryFailures.join("; "),
  );
  console.log(
    testFocus === "shapes"
      ? "PASS browser interaction and boundary checks"
      : testFocus === "empty-pen"
        ? "PASS empty-pen resumed editing and durable PNG checks"
        : testFocus === "source-changed"
          ? "PASS source-changed autosave refusal and artifact preservation"
          : testFocus === "viewer"
            ? "PASS viewer zoom, pan, fullscreen, markup anchoring and alpha badge"
            : testFocus === "video"
              ? "PASS embedded looping video, disabled markup, byte ranges and notes"
              : testFocus === "lifecycle"
                ? "PASS page lifecycle: reload, back/forward restore, hidden tab, leave prompts, close beacon, uploads, canvas warning and Done"
                : "PASS large-image working copy, original-pixel markup/text masks, uncapped-save recovery, full-resolution export, EXIF orientation and source immutability",
  );
} finally {
  if (testFocus === "all" || testFocus === "empty-pen") cli("cleanup");
  if (browser) await browser.close();
  await rm(root, { recursive: true, force: true });
}
