'use strict';
/**
 * Unit tests for renderer/resize-geometry.js.
 *
 * The regression pinned here: when a Left or Top drag pushes the window below
 * the Electron minimum (main.js minWidth 900 / minHeight 600), the size must
 * clamp FIRST and x / y must be derived from the CLAMPED size. Deriving x / y
 * from the raw delta made the window stop resizing yet keep sliding across
 * the screen.
 */
const test = require('node:test');
const assert = require('node:assert');
const { computeResize } = require('../../renderer/resize-geometry.js');

// Mirror of main.js minWidth / minHeight for the BrowserWindow.
const MIN = { w: 900, h: 600 };
const START = { w: 1000, h: 800, x: 200, y: 100 };

// - All 8 edges/corners, normal in-range drag, nothing clamps ---------------
test('each of the 8 edges resizes in range with the opposite edge fixed', () => {
  // [edge, dx, dy, expected]
  const cases = [
    ['Top',          0,  30, { w: 1000, h: 770, x: 200, y: 130 }],
    ['Right',       40,   0, { w: 1040, h: 800, x: 200, y: 100 }],
    ['Bottom',       0, -25, { w: 1000, h: 775, x: 200, y: 100 }],
    ['Left',        30,   0, { w:  970, h: 800, x: 230, y: 100 }],
    ['TopLeft',     10,  15, { w:  990, h: 785, x: 210, y: 115 }],
    ['TopRight',    25,  15, { w: 1025, h: 785, x: 200, y: 115 }],
    ['BottomLeft',  10,  -5, { w:  990, h: 795, x: 210, y: 100 }],
    ['BottomRight', 40, -10, { w: 1040, h: 790, x: 200, y: 100 }],
  ];
  for (const [edge, dx, dy, expected] of cases) {
    assert.deepStrictEqual(
      computeResize(edge, dx, dy, START, MIN), expected,
      `${edge}: dx=${dx} dy=${dy}`);
  }
});

test('Right and Bottom drags never change x or y', () => {
  const r = computeResize('Right', 123, -7, START, MIN);
  assert.strictEqual(r.x, START.x);
  assert.strictEqual(r.y, START.y);
  const b = computeResize('Bottom', -5, 88, START, MIN);
  assert.strictEqual(b.x, START.x);
  assert.strictEqual(b.y, START.y);
});

// - THE regression: Left drag past minWidth pins w AND stops moving x -------
test('regression: Left drag below minWidth clamps w to 900 and freezes x', () => {
  // Raw width would be 1000 - 300 = 700, well below the real minimum 900.
  const first = computeResize('Left', 300, 0, START, MIN);
  assert.strictEqual(first.w, 900, 'width must clamp at main.js minWidth');
  assert.strictEqual(first.x, START.x + START.w - 900,
    'x must come from the CLAMPED width so the right edge stays fixed');

  // An even larger drag must not move the window any further.
  const second = computeResize('Left', 5000, 0, START, MIN);
  assert.strictEqual(second.w, 900);
  assert.strictEqual(second.x, first.x,
    'once pinned at the minimum a larger dx must not slide x further');
});

test('regression: Top drag below minHeight clamps h to 600 and freezes y', () => {
  // Raw height would be 800 - 400 = 400, well below the real minimum 600.
  const first = computeResize('Top', 0, 400, START, MIN);
  assert.strictEqual(first.h, 600, 'height must clamp at main.js minHeight');
  assert.strictEqual(first.y, START.y + START.h - 600,
    'y must come from the CLAMPED height so the bottom edge stays fixed');

  // An even larger drag must not move the window any further.
  const second = computeResize('Top', 0, 5000, START, MIN);
  assert.strictEqual(second.h, 600);
  assert.strictEqual(second.y, first.y,
    'once pinned at the minimum a larger dy must not slide y further');
});

test('TopLeft clamps both axes at once from the clamped size', () => {
  // Raw size would be 750 x 450, both below the minimums 900 x 600.
  const r = computeResize('TopLeft', 250, 350, START, MIN);
  assert.strictEqual(r.w, 900);
  assert.strictEqual(r.h, 600);
  assert.strictEqual(r.x, START.x + START.w - 900,
    'x must be derived from the clamped width');
  assert.strictEqual(r.y, START.y + START.h - 600,
    'y must be derived from the clamped height');
});
