/* The Sites page's "+ Assign…" lists.
 *
 * Every row can give its site to a technician in one pick. Rather than repeat
 * the whole roster in every row, the page carries it once (<template id="roster">)
 * and this copies it into each row's list, leaving out whoever already has that
 * site. It runs again whenever live.js swaps the list for a fresher one.
 *
 * Only option elements are cloned: no name ever passes through a JavaScript
 * string. Without this script the row lists stay hidden and the bar above the
 * list does the same job.
 */
(function () {
  var roster = document.getElementById("roster");
  if (!roster) return;

  function fill() {
    document.querySelectorAll("select[data-roster]:not(.ready)").forEach(function (select) {
      var held = (select.getAttribute("data-held") || "").split(",");
      roster.content.querySelectorAll("option").forEach(function (option) {
        if (held.indexOf(option.value) < 0) select.appendChild(option.cloneNode(true));
      });
      if (select.options.length > 1) select.classList.add("ready");
    });
  }

  fill();
  document.addEventListener("live:swapped", fill);
})();
