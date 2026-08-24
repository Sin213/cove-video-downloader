'use strict';
/**
 * Pure geometry for the custom frameless-window edge resize handles.
 *
 * Clamping happens BEFORE deriving x / y: once the size is pinned at the
 * minimum, the origin stops changing too, so the window cannot slide across
 * the screen while Electron refuses to shrink it below minWidth/minHeight.
 */
(function () {
  function computeResize(edge, dx, dy, start, min) {
    var w = start.w;
    var h = start.h;
    if (edge.indexOf('Left')   !== -1) { w = start.w - dx; }
    if (edge.indexOf('Right')  !== -1) { w = start.w + dx; }
    if (edge.indexOf('Top')    !== -1) { h = start.h - dy; }
    if (edge.indexOf('Bottom') !== -1) { h = start.h + dy; }
    // Clamp first, then derive x / y from the CLAMPED size.
    if (w < min.w) w = min.w;
    if (h < min.h) h = min.h;
    var x = start.x;
    var y = start.y;
    if (edge.indexOf('Left') !== -1) { x = start.x + start.w - w; }
    if (edge.indexOf('Top')  !== -1) { y = start.y + start.h - h; }
    return { w: w, h: h, x: x, y: y };
  }

  if (typeof window !== 'undefined') window.computeResize = computeResize;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { computeResize: computeResize };
  }
})();
