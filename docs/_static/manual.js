/* Page-local links work without JavaScript; enhance location and focus. */
document.addEventListener("DOMContentLoaded", function () {
  "use strict";
  const menu = document.querySelector(".manual-menu-toggle");
  const sidebar = document.querySelector(".wy-nav-side");
  const main = document.getElementById("main-content");
  if (menu && sidebar) {
    const mobile = window.matchMedia("(max-width: 768px)");
    let wasOpen = false;
    sidebar.id = "manual-navigation";
    menu.setAttribute("aria-controls", sidebar.id);
    const syncMenu = function () {
      const open = mobile.matches && sidebar.classList.contains("shift");
      sidebar.inert = mobile.matches && !open;
      if (sidebar.inert) sidebar.setAttribute("aria-hidden", "true");
      else sidebar.removeAttribute("aria-hidden");
      if (main) main.inert = open;
      menu.setAttribute("aria-expanded", String(open));
      menu.setAttribute("aria-label", open ? "关闭章节导航" : "打开章节导航");
      if (open && !wasOpen) (sidebar.querySelector("input[name='q']") || sidebar.querySelector("a")).focus();
      else if (mobile.matches && !open && sidebar.contains(document.activeElement)) menu.focus();
      wasOpen = open;
    };
    const closeMenu = function () {
      document.querySelectorAll("[data-toggle='wy-nav-shift'], [data-toggle='rst-versions']")
        .forEach(function (element) { element.classList.remove("shift"); });
      syncMenu();
    };
    syncMenu();
    new MutationObserver(syncMenu).observe(sidebar, {attributes: true, attributeFilter: ["class"]});
    mobile.addEventListener("change", function () { closeMenu(); });
    document.addEventListener("keydown", function (event) {
      if (!mobile.matches || !sidebar.classList.contains("shift")) return;
      if (event.key === "Escape") {
        closeMenu();
        menu.focus();
      } else if (event.key === "Tab") {
        const focusable = Array.from(sidebar.querySelectorAll("a[href], button, input"))
          .filter(function (element) { return !element.disabled && element.getClientRects().length > 0; });
        focusable.push(menu);
        const index = focusable.indexOf(document.activeElement);
        if (index < 0 || (!event.shiftKey && index === focusable.length - 1) || (event.shiftKey && index === 0)) {
          event.preventDefault();
          focusable[event.shiftKey ? focusable.length - 1 : 0].focus();
        }
      }
    });
    sidebar.addEventListener("click", function (event) {
      if (event.target.closest("a") && !event.target.closest("button") && window.innerWidth <= 768) closeMenu();
    });
  }
  document.querySelectorAll(".manual-mobile-toc a").forEach(function (link) {
    link.addEventListener("click", function () { link.closest("details").open = false; });
  });
  const skip = document.querySelector(".manual-skip");
  if (skip && main) skip.addEventListener("click", function () { main.focus(); });
  const entries = Array.from(document.querySelectorAll(".manual-toc a.reference.internal"))
    .map(function (link) {
      const hash = link.getAttribute("href");
      let target = null;
      try { target = hash && hash.startsWith("#") ? document.getElementById(decodeURIComponent(hash.slice(1))) : null; }
      catch (_) { /* Ignore malformed fragment links from third-party extensions. */ }
      return {link: link, target: target};
    }).filter(function (entry) { return entry.target; });
  let scheduled = false;
  function updateLocation() {
    scheduled = false;
    let current = entries[0];
    entries.forEach(function (entry) {
      if (entry.target.getBoundingClientRect().top <= 140) current = entry;
    });
    // A short final section cannot reach the top once the page is at its end.
    if (window.scrollY > 0 && Math.ceil(window.scrollY + window.innerHeight) >= document.documentElement.scrollHeight - 2) {
      current = entries[entries.length - 1];
    }
    entries.forEach(function (entry) {
      if (entry === current) entry.link.setAttribute("aria-current", "location");
      else entry.link.removeAttribute("aria-current");
    });
  }
  if (entries.length) {
    window.addEventListener("scroll", function () {
      if (!scheduled) { scheduled = true; window.requestAnimationFrame(updateLocation); }
    }, {passive: true});
    window.addEventListener("resize", updateLocation);
    window.addEventListener("hashchange", updateLocation);
    updateLocation();
  }
});
