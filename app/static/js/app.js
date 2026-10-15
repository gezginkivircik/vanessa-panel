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

  // ---------------------------------------------------------------- randevu: müşteri seçici
  document.querySelectorAll("[data-customer-picker]").forEach(function (root) {
    var idInput = root.querySelector("[data-customer-id]");
    var chip = root.querySelector("[data-customer-chip]");
    var searchBox = root.querySelector("[data-customer-search]");
    var input = root.querySelector("[data-customer-input]");
    var list = root.querySelector("[data-customer-results]");
    var t = null;
    function pick(r) {
      idInput.value = r.id;
      root.querySelector("[data-customer-name]").textContent = r.name;
      root.querySelector("[data-customer-initials]").textContent =
        r.name.split(" ").filter(Boolean).map(function (w) { return w[0]; }).slice(0, 2).join("");
      chip.hidden = false; searchBox.hidden = true; list.hidden = true;
    }
    root.querySelector("[data-customer-clear]").addEventListener("click", function () {
      idInput.value = ""; chip.hidden = true; searchBox.hidden = false; input.value = ""; input.focus();
    });
    input.addEventListener("input", function () {
      var q = input.value.trim(); clearTimeout(t);
      if (q.length < 2) { list.hidden = true; return; }
      t = setTimeout(function () {
        fetch(root.dataset.endpoint + "?q=" + encodeURIComponent(q), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            list.textContent = "";
            (d.results || []).forEach(function (r) {
              var b = document.createElement("button");
              b.type = "button"; b.className = "gsearch__item";
              var s = document.createElement("strong"); s.textContent = r.name;
              var p = document.createElement("span"); p.textContent = r.phone;
              b.appendChild(s); b.appendChild(p);
              b.addEventListener("click", function () { pick(r); });
              list.appendChild(b);
            });
            if (!(d.results || []).length) {
              var e = document.createElement("div"); e.className = "gsearch__empty"; e.textContent = "Müşteri bulunamadı.";
              list.appendChild(e);
            }
            list.hidden = false;
          });
      }, 180);
    });
  });

  // ---------------------------------------------------------------- randevu: uygun saat seçici
  document.querySelectorAll("[data-slot-picker]").forEach(function (picker) {
    var form = picker.closest("form");
    var service = form.querySelector("[data-service]");
    var date = form.querySelector("[data-date]");
    var staffOut = form.querySelector("[data-slot-staff]");
    var timeOut = form.querySelector("[data-slot-time]");
    var filter = form.querySelector("[data-staff-filter]");
    var mapEl = document.getElementById("service-staff");
    var serviceStaff = mapEl ? JSON.parse(mapEl.textContent) : null;
    var seq = 0;

    function pref() {
      var r = filter && filter.querySelector("input[type=radio]:checked");
      return r ? r.value : "";
    }
    function syncStaffChips() {
      if (!serviceStaff || !filter) return;
      var allowed = (serviceStaff[service.value] || []).map(String);
      filter.querySelectorAll("[data-staff-chip]").forEach(function (c) {
        var ok = !service.value || allowed.indexOf(c.dataset.staffChip) >= 0;
        c.hidden = !ok;
        var inp = c.querySelector("input");
        if (!ok && inp.checked) { filter.querySelector("input[value='']").checked = true; }
      });
    }
    function msg(text) { picker.textContent = ""; var p = document.createElement("p"); p.className = "muted small"; p.textContent = text; picker.appendChild(p); }
    function load() {
      timeOut.value = "";
      if (!service.value || !date.value) { msg("Önce hizmet ve tarih seçin."); return; }
      var url = picker.dataset.endpoint + "?hizmet=" + service.value + "&tarih=" + date.value;
      if (pref()) url += "&uzman=" + pref();
      if (picker.dataset.exclude) url += "&haric=" + picker.dataset.exclude;
      var my = ++seq;
      msg("Uygun saatler yükleniyor…");
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
        if (my !== seq) return;
        picker.textContent = "";
        var any = false;
        (d.staff || []).forEach(function (s) {
          var group = document.createElement("div"); group.className = "slots__group";
          var h = document.createElement("div"); h.className = "slots__staff";
          var dot = document.createElement("span"); dot.className = "dot"; dot.style.background = s.color;
          h.appendChild(dot); h.appendChild(document.createTextNode(" " + s.staff_name));
          group.appendChild(h);
          var row = document.createElement("div"); row.className = "slots__row";
          if (!s.slots.length) {
            var none = document.createElement("span"); none.className = "muted small"; none.textContent = "Bu gün uygun saat yok.";
            row.appendChild(none);
          }
          s.slots.forEach(function (t) {
            any = true;
            var b = document.createElement("button");
            b.type = "button"; b.className = "slot"; b.textContent = t;
            b.addEventListener("click", function () {
              picker.querySelectorAll(".slot.is-selected").forEach(function (x) { x.classList.remove("is-selected"); });
              b.classList.add("is-selected");
              staffOut.value = s.staff_id; timeOut.value = t;
            });
            row.appendChild(b);
          });
          group.appendChild(row);
          picker.appendChild(group);
        });
        if (!(d.staff || []).length) msg("Bu hizmeti veren uzman bulunamadı.");
        else if (!any) {
          var p = document.createElement("p"); p.className = "small"; p.style.color = "var(--st-cancelled)";
          p.textContent = "Seçilen günde boş saat yok. Başka bir gün deneyin.";
          picker.appendChild(p);
        }
      }).catch(function () { msg("Saatler yüklenemedi. Tekrar deneyin."); });
    }
    if (service.tagName === "SELECT") service.addEventListener("change", function () { syncStaffChips(); load(); });
    date.addEventListener("change", load);
    if (filter) filter.addEventListener("change", load);
    form.querySelectorAll("[data-date-shift]").forEach(function (b) {
      b.addEventListener("click", function () {
        var d = new Date(); d.setDate(d.getDate() + parseInt(b.dataset.dateShift, 10));
        date.value = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
        load();
      });
    });
    form.addEventListener("submit", function (e) {
      if (!timeOut.value) {
        e.preventDefault(); delete form.dataset.submitting;
        msg("Lütfen uygun bir saat seçin."); load();
      }
    });
    syncStaffChips();
    if (service.value && date.value) load();
  });

  // Çift gönderimi engelle
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented) return;
    if (form.dataset.submitting) { e.preventDefault(); return; }
    form.dataset.submitting = "1";
    setTimeout(function () { delete form.dataset.submitting; }, 4000);
  });
})();

// Web sitesi: kaydırınca üst menüye ince çizgi
(function () {
  var nav = document.querySelector("[data-sticky-nav]");
  if (!nav) return;
  function upd() { nav.classList.toggle("is-scrolled", window.scrollY > 8); }
  window.addEventListener("scroll", upd, { passive: true });
  upd();
})();

// Online randevu: seçili günü görünür alana kaydır
(function () {
  var sel = document.querySelector('.days [aria-current="true"]');
  if (sel && sel.parentNode) sel.parentNode.scrollLeft = sel.offsetLeft - sel.parentNode.offsetLeft - 12;
})();
