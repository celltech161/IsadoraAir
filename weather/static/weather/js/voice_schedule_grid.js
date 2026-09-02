/* r0028: progressive-enhancement layer for the 24-hour announcer
 * schedule grid. Vanilla JS, no build step, no framework.
 *
 * Everything this script does is optional: the 24 real <select>
 * elements rendered by weather/widgets/hourly_schedule.html already
 * fully work -- name/value pairs, keyboard navigation, screen readers
 * -- with zero JS. This script only adds two conveniences:
 *   1. click-and-drag "painting" of a chosen persona across hours,
 *      via each hour's LABEL (never the <select> itself, so the
 *      native picker always remains directly usable/keyboard-only);
 *   2. a lightweight, non-exclusive colour cue per persona (never the
 *      only indicator -- the select's own text is authoritative).
 * If this file fails to load or throws, the page underneath is
 * unchanged and fully functional.
 */
(function () {
  "use strict";

  var PALETTE = [
    "#f6c453", "#6fb1e6", "#8fd694", "#e08fd6",
    "#e0a26f", "#9aa6e0", "#e07f7f", "#7fe0c9",
  ];

  function initGrid(root) {
    var select = function (sel, ctx) { return (ctx || root).querySelector(sel); };
    var selectAll = function (sel, ctx) { return Array.prototype.slice.call((ctx || root).querySelectorAll(sel)); };

    var legendItems = selectAll(".wx-legend-item");
    var cells = selectAll(".wx-hour-cell");
    var activePaintSlot = null;

    // -- colour cue (never the only indicator; the text label always
    // stands on its own) --------------------------------------------
    var colorBySlot = {};
    legendItems.forEach(function (item, index) {
      var slotClass = Array.prototype.filter.call(item.classList, function (c) {
        return c.indexOf("wx-slot-") === 0;
      })[0];
      if (!slotClass) return;
      var slot = slotClass.slice("wx-slot-".length);
      var color = PALETTE[index % PALETTE.length];
      colorBySlot[slot] = color;
      var swatch = select(".wx-swatch", item);
      if (swatch) swatch.style.background = color;
    });

    function applyCellColor(cell) {
      var value = select("select", cell).value;
      cell.style.background = value && colorBySlot[value] ? colorBySlot[value] + "33" : "";
      cell.style.borderColor = value && colorBySlot[value] ? colorBySlot[value] : "";
    }
    cells.forEach(applyCellColor);

    // -- palette: click a legend entry to choose what "painting" ------
    // an hour assigns. Purely a convenience; every hour's own <select>
    // remains the authoritative, always-available way to set it.
    legendItems.forEach(function (item) {
      item.setAttribute("tabindex", "0");
      item.setAttribute("role", "button");
      var slotClass = Array.prototype.filter.call(item.classList, function (c) {
        return c.indexOf("wx-slot-") === 0;
      })[0];
      var slot = slotClass ? slotClass.slice("wx-slot-".length) : null;
      if (!slot) return;
      item.setAttribute("aria-pressed", "false");

      var activate = function () {
        legendItems.forEach(function (other) {
          other.setAttribute("aria-pressed", "false");
          other.style.outline = "";
        });
        if (activePaintSlot === slot) {
          activePaintSlot = null; // click again to deselect
          return;
        }
        activePaintSlot = slot;
        item.setAttribute("aria-pressed", "true");
        item.style.outline = "2px solid " + (colorBySlot[slot] || "#333");
      };
      item.addEventListener("click", activate);
      item.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
    });

    // -- paint one hour with the active persona, keeping the real
    // <select> as the single source of truth (dispatch "change" so
    // anything else listening -- browser autofill, etc. -- behaves
    // normally). ------------------------------------------------------
    function paintCell(cell) {
      if (!activePaintSlot) return;
      var sel = select("select", cell);
      if (sel.value !== activePaintSlot) {
        sel.value = activePaintSlot;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
      applyCellColor(cell);
    }

    var dragging = false;
    cells.forEach(function (cell) {
      var label = select("label", cell);
      if (!label) return;
      label.addEventListener("mousedown", function (event) {
        if (!activePaintSlot) return; // no active paint -- let normal focus/click happen
        event.preventDefault();
        dragging = true;
        paintCell(cell);
      });
      label.addEventListener("mouseenter", function () {
        if (dragging) paintCell(cell);
      });
      var sel = select("select", cell);
      sel.addEventListener("change", function () { applyCellColor(cell); });
    });
    document.addEventListener("mouseup", function () { dragging = false; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.slice.call(document.querySelectorAll(".wx-schedule-grid")).forEach(initGrid);
  });
})();
