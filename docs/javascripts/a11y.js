/*
 * Accessibility fixes for the theme's shared chrome, loaded on every page.
 *
 * Material renders the navigation-drawer and search toggles, the drawer's
 * "back" titles and some of its section entries as <label for> elements bound
 * to hidden checkboxes. A label is not focusable and has no role, so below the
 * desktop breakpoint a keyboard or screen-reader user could not open the
 * navigation or move around inside it. This script:
 *
 *   - gives those labels the role, name, state and key handling of a button;
 *   - treats the open drawer as a dialog: focus moves in, Tab cycles inside
 *     it, the page it covers is inert, and Escape closes it and returns focus;
 *   - keeps focus on a visible control when a nested panel opens or closes.
 *
 * stylesheets/anvil.css hides the closed drawer and its closed panels from the
 * tab order. The script also marks the current page in the navigation with
 * aria-current, which the theme signals by colour only.
 */
(function () {
  "use strict";

  // Matches the theme's own breakpoint for the drawer layout.
  var NARROW = window.matchMedia("(max-width: 76.234375em)");

  function each(nodes, fn) {
    Array.prototype.forEach.call(nodes, fn);
  }

  // The theme already activates a focused label on Enter. Space is the half of
  // button behaviour it leaves out; handling Enter here as well would click
  // twice and close what was just opened.
  function onSpace(label) {
    label.addEventListener("keydown", function (event) {
      if (event.key === " ") {
        event.preventDefault();
        label.click();
      }
    });
  }

  function setChecked(toggle, checked) {
    if (toggle.checked === checked) return;
    toggle.checked = checked;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // The controls a keyboard user can actually reach inside a container right
  // now: rendered, and not in a panel the stylesheet has hidden.
  function visibleControls(root) {
    return Array.prototype.filter.call(root.querySelectorAll('a[href], [tabindex="0"]'), function (node) {
      return node.getClientRects().length && getComputedStyle(node).visibility === "visible";
    });
  }

  // Drawer panels slide in with a transform. Focusing a control in a panel
  // that is still moving makes the browser scroll the drawer sideways to
  // reveal it, which leaves the drawer blank; so focus never scrolls here.
  function focusQuietly(node) {
    if (node) node.focus({ preventScroll: true });
  }

  function start() {
    var drawer = document.getElementById("__drawer");
    var drawerLabel = document.querySelector('.md-header__button[for="__drawer"]');
    var sidebar = document.querySelector(".md-sidebar--primary");

    if (drawer && drawerLabel && sidebar) {
      drawerLabel.setAttribute("role", "button");
      drawerLabel.setAttribute("tabindex", "0");
      drawerLabel.setAttribute("aria-label", "Navigation");
      drawerLabel.setAttribute("aria-expanded", "false");
      onSpace(drawerLabel);

      // Inside the drawer: each nested panel's "back" title, and the section
      // entries the theme leaves unfocusable (the active section and the
      // current page's table of contents). They only act as controls in the
      // drawer layout, so they only get the role and a tab stop there.
      var panelLabels = Array.prototype.filter.call(
        sidebar.querySelectorAll(".md-nav .md-nav label.md-nav__title[for], .md-nav label.md-nav__link[for]"),
        function (label) {
          return label.getAttribute("tabindex") !== "0";
        }
      );
      each(panelLabels, onSpace);

      var applyLayout = function () {
        each(panelLabels, function (label) {
          if (NARROW.matches) {
            label.setAttribute("role", "button");
            label.setAttribute("tabindex", "0");
          } else {
            label.removeAttribute("role");
            label.removeAttribute("tabindex");
          }
        });
        // Leaving the drawer layout with the drawer open would strand the
        // page behind it as inert.
        if (!NARROW.matches) setChecked(drawer, false);
      };
      applyLayout();
      if (NARROW.addEventListener) NARROW.addEventListener("change", applyLayout);

      // While the drawer is open it covers the page; the page leaves the tab
      // order with it.
      var behind = document.querySelectorAll(".md-content, .md-footer, .md-sidebar--secondary");
      drawer.addEventListener("change", function () {
        var open = drawer.checked;
        drawerLabel.setAttribute("aria-expanded", open ? "true" : "false");
        each(behind, function (node) {
          if (open) node.setAttribute("inert", "");
          else node.removeAttribute("inert");
        });
        if (open) {
          window.requestAnimationFrame(function () {
            focusQuietly(visibleControls(sidebar)[0]);
          });
        }
      });

      // Opening or leaving a nested panel hides the control that was just
      // used, so focus follows: into the panel that opened, or back to the
      // entry that leads to the one that closed.
      sidebar.addEventListener("change", function (event) {
        var toggle = event.target;
        if (!drawer.checked || !toggle.classList || !toggle.classList.contains("md-nav__toggle")) return;
        window.requestAnimationFrame(function () {
          var target = null;
          if (toggle.checked) {
            var panel = toggle.parentElement.querySelector(":scope > .md-nav");
            target = panel && visibleControls(panel)[0];
          } else {
            target = sidebar.querySelector('label.md-nav__link[for="' + toggle.id + '"]');
            if (target && getComputedStyle(target).visibility !== "visible") target = null;
          }
          focusQuietly(target || visibleControls(sidebar)[0] || drawerLabel);
        });
      });

      document.addEventListener("keydown", function (event) {
        if (!drawer.checked) return;
        if (event.key === "Escape") {
          setChecked(drawer, false);
          drawerLabel.focus();
          return;
        }
        // The open drawer behaves as a dialog: Tab cycles within it instead of
        // wandering to controls it covers.
        if (event.key === "Tab") {
          var controls = visibleControls(sidebar);
          if (!controls.length) return;
          var first = controls[0];
          var last = controls[controls.length - 1];
          var inside = sidebar.contains(document.activeElement);
          if (event.shiftKey && (!inside || document.activeElement === first)) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && (!inside || document.activeElement === last)) {
            event.preventDefault();
            first.focus();
          }
        }
      });
    }

    var searchLabel = document.querySelector('.md-header__button[for="__search"]');
    if (searchLabel) {
      searchLabel.setAttribute("role", "button");
      searchLabel.setAttribute("tabindex", "0");
      searchLabel.setAttribute("aria-label", "Search");
      onSpace(searchLabel);
    }

    each(document.querySelectorAll("a.md-nav__link--active"), function (link) {
      link.setAttribute("aria-current", "page");
    });
    each(document.querySelectorAll(".md-tabs__item--active > .md-tabs__link"), function (link) {
      link.setAttribute("aria-current", "true");
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
