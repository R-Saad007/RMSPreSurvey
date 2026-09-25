/* Keeps a page current without a reload.
 *
 * Every few seconds it asks /live/version whether anything this user can see
 * has changed (the answer is scoped on the server: a company's pages only
 * move for that company's sites). When it has, it fetches this same page again
 * and swaps in the fresh contents of each element marked data-live — nothing
 * else, so forms and anything typed stay put. A region that holds the cursor,
 * or one someone has typed into, is left alone until the next change.
 */
(function () {
  var main = document.querySelector("main[data-live-version]");
  if (!main || !window.fetch || !window.DOMParser) return;
  var version = main.getAttribute("data-live-version");
  var edited = [];

  function markEdited(event) {
    var region = event.target.closest && event.target.closest("[data-live]");
    if (region && edited.indexOf(region) < 0) edited.push(region);
  }
  document.addEventListener("input", markEdited);
  document.addEventListener("change", markEdited);

  function busy(region) {
    return edited.indexOf(region) >= 0 || region.contains(document.activeElement);
  }

  function swap(fresh) {
    document.querySelectorAll("[data-live][id]").forEach(function (old) {
      var next = fresh.getElementById(old.id);
      if (!next || busy(old)) return;
      var open = [];
      old.querySelectorAll("details[open][id]").forEach(function (d) { open.push(d.id); });
      old.innerHTML = next.innerHTML;
      open.forEach(function (id) {
        var d = document.getElementById(id);
        if (d) d.open = true;
      });
    });
  }

  var running = false;
  function tick() {
    if (running || document.visibilityState !== "visible") return;
    running = true;
    fetch("/live/version", { cache: "no-store", credentials: "same-origin", redirect: "manual" })
      .then(function (r) { return r.status === 200 ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.v || data.v === version) return null;
        var next = data.v;
        return fetch(location.href, { cache: "no-store", credentials: "same-origin", redirect: "manual" })
          .then(function (r) {
            var type = r.headers.get("content-type") || "";
            return r.status === 200 && type.indexOf("text/html") === 0 ? r.text() : null;
          })
          .then(function (html) {
            if (!html) return;
            swap(new DOMParser().parseFromString(html, "text/html"));
            version = next;
          });
      })
      .catch(function () { /* offline, or signed out: try again next time */ })
      .then(function () { running = false; });
  }

  setInterval(tick, 5000);
  document.addEventListener("visibilitychange", tick);
})();
