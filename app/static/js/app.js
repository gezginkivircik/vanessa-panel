// Vanessa paneli — küçük, bağımlılıksız etkileşimler.
(function () {
  "use strict";
  var drawer = document.getElementById("drawer");

  function openDrawer() {
    if (!drawer) return;
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
    var first = drawer.querySelector("a:not([aria-disabled]), button");
    if (first) first.focus();
  }
  function closeDrawer() {
    if (!drawer) return;
    drawer.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-open-drawer]");
    if (t) { e.preventDefault(); openDrawer(); return; }
    if (e.target.closest("[data-close-drawer]")) { closeDrawer(); return; }
    var c = e.target.closest("[data-confirm]");
    if (c && !window.confirm(c.getAttribute("data-confirm"))) { e.preventDefault(); }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDrawer(); });

  // Global hızlı müşteri arama (isim / telefon)
  var box = document.querySelector("[data-search]");
  if (box) {
    var input = box.querySelector("[data-search-input]");
    var results = box.querySelector("[data-search-results]");
    var timer = null, seq = 0, active = -1;

    function el(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    function hide() { results.hidden = true; active = -1; }
    function items() { return results.querySelectorAll(".gsearch__item"); }
    function mark(i) {
      var list = items();
      list.forEach(function (n, j) { n.classList.toggle("is-active", j === i); });
      active = i;
    }
    function render(q, data) {
      results.textContent = "";
      if (!data.length) {
        var empty = el("div", "gsearch__empty", "“" + q + "” için müşteri bulunamadı. ");
        if (input.dataset.newUrl) {
          var a = el("a", null, "+ Yeni müşteri ekle");
          a.href = input.dataset.newUrl + "?q=" + encodeURIComponent(q);
          empty.appendChild(a);
        }
        results.appendChild(empty);
      } else {
        data.forEach(function (r) {
          var a = el("a", "gsearch__item");
          a.href = r.url; a.setAttribute("role", "option");
          a.appendChild(el("strong", null, r.name));
          a.appendChild(el("span", null, r.phone));
          results.appendChild(a);
        });
      }
      results.hidden = false; active = -1;
    }
    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(timer);
      if (q.length < 2) { hide(); return; }
      timer = setTimeout(function () {
        var my = ++seq;
        fetch(input.dataset.endpoint + "?q=" + encodeURIComponent(q), { credentials: "same-origin", headers: { "Accept": "application/json" } })
          .then(function (r) { return r.ok ? r.json() : { results: [] }; })
          .then(function (d) { if (my === seq) render(q, d.results || []); })
          .catch(function () {});
      }, 180);
    });
    input.addEventListener("keydown", function (e) {
      var list = items();
      if (e.key === "ArrowDown" && list.length) { e.preventDefault(); mark(Math.min(active + 1, list.length - 1)); }
      else if (e.key === "ArrowUp" && list.length) { e.preventDefault(); mark(Math.max(active - 1, 0)); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (active >= 0 && list[active]) { window.location = list[active].href; }
        else if (list.length === 1) { window.location = list[0].href; }
        else if (input.value.trim()) { window.location = input.dataset.listUrl + "?q=" + encodeURIComponent(input.value.trim()); }
      } else if (e.key === "Escape") { hide(); input.blur(); }
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target)) hide(); });
  }

  // Çift gönderimi engelle
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.dataset.submitting) { e.preventDefault(); return; }
    form.dataset.submitting = "1";
    setTimeout(function () { delete form.dataset.submitting; }, 4000);
  });
})();
