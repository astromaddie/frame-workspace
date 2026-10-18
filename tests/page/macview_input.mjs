// Exercise the production viewer input handlers with actual contain geometry.
// No copy of the coordinate transform or wheel handler lives in this test.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../../ui/mac-view.html", import.meta.url), "utf8");
const start = source.indexOf("// ---- input ----");
const end = source.indexOf("function mods(e)", start);
assert.ok(start >= 0 && end > start, "production input block exists");

function viewer(width, height, rect) {
  const handlers = new Map(), sent = [];
  const env = {
    c: { width, height, getBoundingClientRect: () => rect, setPointerCapture() {} },
    addEventListener: (name, callback) => handlers.set(name, callback),
    send: message => sent.push(message),
    performance: { now: () => 100 }, toMac: value => value,
    innerHeight: rect.height, setTimeout, setInterval, clearInterval,
  };
  new Function(...Object.keys(env), source.slice(start, end))(...Object.values(env));
  const fire = (name, x, y, extra = {}) => {
    const event = { clientX: x, clientY: y, deltaX: 2, deltaY: 3,
      deltaMode: 0, button: 0, pointerId: 1, timeStamp: 100,
      prevented: false, preventDefault() { this.prevented = true; }, ...extra };
    handlers.get(name)(event);
    return event;
  };
  return { sent, fire };
}

// 16:9 picture in a 4:3 canvas with a nonzero page origin: 60px bars.
{
  const p = viewer(1920, 1080, { left: 30, top: 20, width: 640, height: 480 });
  p.fire("wheel", 350, 40); p.fire("wheel", 350, 480);
  p.fire("pointerdown", 350, 40);
  assert.equal(p.sent.length, 0, "bars must not scroll or click the Mac source");
  const event = p.fire("wheel", 350, 260, { deltaMode: 1 });
  assert.equal(p.sent.length, 1);
  assert.equal(p.sent[0].t, "wheel");
  assert.equal(p.sent[0].x, 0.5); assert.equal(p.sent[0].y, 0.5);
  assert.equal(p.sent[0].dx, 32); assert.equal(p.sent[0].dy, 48);
  assert.equal(event.prevented, true);
  // Releasing outside the picture still ends an in-picture drag.
  p.fire("pointerdown", 350, 260);
  p.fire("pointerup", 350, 40);
  assert.equal(p.sent.at(-1).e, "up");
  assert.ok(p.sent.at(-1).y < 0);
}
// Portrait picture in a wide canvas: side bars must not scroll the source.
{
  const p = viewer(2560, 2880, { left: 0, top: 0, width: 1000, height: 600 });
  p.fire("wheel", 5, 300); p.fire("wheel", 995, 300);
  assert.equal(p.sent.length, 0);
  p.fire("wheel", 500, 300, { deltaMode: 2 });
  assert.equal(p.sent.length, 1);
  assert.equal(p.sent[0].x, 0.5); assert.equal(p.sent[0].y, 0.5);
  assert.equal(p.sent[0].dy, 1800);
  p.fire("pointercancel", 500, 300);
  assert.equal(p.sent.at(-1).t, "release");
}
console.log("PASS MacView picture geometry, wheel bounds, and outside drag release");
