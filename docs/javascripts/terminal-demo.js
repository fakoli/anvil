/*
 * Terminal replay for the landing page.
 *
 * Plays back docs/demo/session.json — a real anvil session captured by
 * docs/demo/record.sh — as typed commands and their output. Nothing here
 * invents text: every command and every line of output comes from that file.
 *
 * Behaviour that matters for accessibility:
 *   - It never starts by itself for people who ask for reduced motion; they
 *     get the first step rendered in full and step through with the buttons.
 *   - Playback can always be paused (WCAG 2.2.2), and it pauses itself when
 *     scrolled out of view.
 *   - Every step is reachable from the step list and the previous / next
 *     buttons, by keyboard, without waiting for the animation.
 *   - Output is real text in the DOM, not a canvas or a video.
 */
(function () {
  "use strict";

  var REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function Terminal(root) {
    this.root = root;
    this.screen = root.querySelector("[data-term-screen]");
    this.stepsEl = root.querySelector("[data-term-steps]");
    this.toggleBtn = root.querySelector("[data-term-toggle]");
    this.prevBtn = root.querySelector("[data-term-prev]");
    this.nextBtn = root.querySelector("[data-term-next]");
    this.statusEl = root.querySelector("[data-term-status]");
    this.captionEl = root.querySelector("[data-term-caption]");
    this.countEl = root.querySelector("[data-term-count]");
    this.steps = [];
    this.index = 0;
    this.playing = false;
    this.started = false; // true once playback or navigation has happened
    this.finished = false;
    this.autoPaused = false;
    this.touched = false;
    this.timer = null;
    this.run = 0; // bumps on every interruption so stale timers do nothing
  }

  Terminal.prototype.load = function () {
    var self = this;
    var src = this.root.getAttribute("data-src");
    return fetch(src, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (steps) {
        if (!Array.isArray(steps) || !steps.length) throw new Error("empty session");
        self.steps = steps;
        self.build();
      })
      .catch(function () {
        self.root.classList.add("is-unavailable");
        self.screen.textContent = "";
        var note = el("p", "term__note");
        note.textContent = "The recorded session could not be loaded. ";
        var link = el("a", null, "Follow the same walkthrough in the getting-started guide");
        link.href = self.root.getAttribute("data-fallback-href") || "how-to/getting-started/";
        note.appendChild(link);
        note.appendChild(document.createTextNode("."));
        self.screen.appendChild(note);
        [self.toggleBtn, self.prevBtn, self.nextBtn].forEach(function (button) {
          if (button) button.disabled = true;
        });
      });
  };

  Terminal.prototype.build = function () {
    var self = this;

    // The rail groups steps into acts ("Plan", "Coordinate", ...). Only the
    // act that holds the current step is open, so the list stays short.
    this.items = [];
    this.acts = [];
    var act = null;
    this.steps.forEach(function (step, i) {
      var name = step.act || "Session";
      if (!act || act.name !== name) {
        act = { name: name, first: i, count: 0, node: el("li", "term__act") };
        act.button = el("button", "term__act-btn");
        act.button.type = "button";
        act.button.appendChild(el("span", "term__act-name", name));
        act.meta = el("span", "term__act-meta");
        act.button.appendChild(act.meta);
        act.list = el("ol", "term__act-steps");
        (function (first) {
          act.button.addEventListener("click", function () {
            self.touched = true;
            self.goTo(first, { announce: true });
          });
        })(i);
        act.node.appendChild(act.button);
        act.node.appendChild(act.list);
        self.stepsEl.appendChild(act.node);
        self.acts.push(act);
      }
      act.count += 1;
      var item = el("li", "term__step");
      var button = el("button", "term__step-btn");
      button.type = "button";
      button.appendChild(el("span", "term__step-num", String(i + 1).padStart(2, "0")));
      button.appendChild(el("span", "term__step-label", step.label || step.comment));
      button.addEventListener("click", function () {
        self.touched = true;
        self.goTo(i, { announce: true });
      });
      item.appendChild(button);
      act.list.appendChild(item);
      self.items.push({ node: item, button: button, act: act });
    });
    this.acts.forEach(function (a) {
      a.meta.textContent = a.count + (a.count === 1 ? " step" : " steps");
    });

    this.toggleBtn.addEventListener("click", function () {
      self.touched = true;
      if (self.playing) self.pause();
      else self.play();
    });
    // At either end these are aria-disabled rather than disabled, so the
    // button that was just pressed keeps focus when it reaches the last step.
    this.prevBtn.addEventListener("click", function () {
      if (self.index === 0) return;
      self.touched = true;
      self.goTo(self.index - 1, { announce: true });
    });
    this.nextBtn.addEventListener("click", function () {
      if (self.index === self.steps.length - 1) return;
      self.touched = true;
      self.goTo(self.index + 1, { announce: true });
    });

    // Reduced motion: no autoplay and no typing, so the play control goes away
    // and the first step is simply shown.
    function applyMotionPreference() {
      self.root.classList.toggle("is-static", REDUCED.matches);
      self.toggleBtn.hidden = REDUCED.matches;
      if (REDUCED.matches) self.pause();
    }
    applyMotionPreference();
    if (REDUCED.addEventListener) REDUCED.addEventListener("change", applyMotionPreference);

    this.renderThrough(0);
    this.sync();
    this.root.classList.add("is-ready");

    if ("IntersectionObserver" in window) {
      new IntersectionObserver(
        function (entries) {
          var visible = entries[0].isIntersecting;
          var mayStart = !self.touched && !self.started;
          if (visible && !self.playing && !REDUCED.matches && (mayStart || self.autoPaused)) {
            self.autoPaused = false;
            self.play();
          } else if (!visible && self.playing) {
            self.pause();
            self.autoPaused = true;
          }
        },
        { threshold: 0.35 }
      ).observe(this.root);
    }
  };

  /* ---- rendering ------------------------------------------------------ */

  Terminal.prototype.entry = function (step) {
    var entry = el("div", "term__entry");
    entry.appendChild(el("div", "term__comment", "# " + step.comment));
    var line = el("div", "term__cmd");
    line.appendChild(el("span", "term__prompt", "$ "));
    var typed = el("span", "term__typed");
    line.appendChild(typed);
    entry.appendChild(line);
    return { entry: entry, typed: typed, line: line };
  };

  // Tone is presentation only: the words already say OK, PASSED or REFUSED,
  // and the text itself is never changed.
  function toneFor(line) {
    if (/Signed proof:/.test(line)) return "is-proof";
    if (/^Error:|REFUSED|Evidence gate: INCOMPLETE/.test(line)) return "is-bad";
    if (/^\s*\[OK\]|Evidence gate: PASSED|→ done\.|^Overall: healthy/.test(line)) return "is-ok";
    return "";
  }

  Terminal.prototype.output = function (step) {
    var fragment = document.createDocumentFragment();
    var text = step.output || "";
    var hidden = 0;
    if (Array.isArray(step.display_lines)) {
      var lines = text.split("\n");
      var shown = lines.slice(step.display_lines[0], step.display_lines[1]);
      hidden = lines.length - shown.length;
      text = shown.join("\n");
    }
    if (text) {
      var pre = el("pre", "term__out" + (step.exit ? " is-fail" : ""));
      text.split("\n").forEach(function (line, i, all) {
        var tone = toneFor(line);
        pre.appendChild(tone ? el("span", tone, line) : document.createTextNode(line));
        if (i < all.length - 1) pre.appendChild(document.createTextNode("\n"));
      });
      fragment.appendChild(pre);
    }
    if (hidden > 0) {
      // Shortening is always declared; the full text is in session.json.
      fragment.appendChild(el("div", "term__more", "… " + hidden + " more lines"));
    }
    if (step.exit) {
      // A failure is stated as text, not only by the colour of the output.
      fragment.appendChild(el("div", "term__exit", "exit " + step.exit));
    }
    return fragment;
  };

  Terminal.prototype.renderFull = function (step) {
    var parts = this.entry(step);
    parts.typed.textContent = step.cmd;
    parts.entry.appendChild(this.output(step));
    this.screen.appendChild(parts.entry);
  };

  // Show steps 0..i complete, with no animation.
  Terminal.prototype.renderThrough = function (i) {
    this.screen.textContent = "";
    for (var k = 0; k <= i; k++) this.renderFull(this.steps[k]);
    this.index = i;
    this.scrollToLatest();
  };

  Terminal.prototype.scrollToLatest = function () {
    var last = this.screen.lastElementChild;
    if (!last) return;
    // Keep the newest command at the top of the viewport when its output is
    // taller than the screen; otherwise pin to the bottom like a terminal.
    // (.term__screen is the offset parent, so offsetTop is already relative.)
    var bottom = this.screen.scrollHeight - this.screen.clientHeight;
    this.screen.scrollTop = Math.min(last.offsetTop, bottom);
  };

  Terminal.prototype.sync = function () {
    var self = this;
    var step = this.steps[this.index];
    var currentAct = this.items[this.index].act;
    this.items.forEach(function (item, i) {
      item.node.classList.toggle("is-current", i === self.index);
      item.node.classList.toggle("is-done", i < self.index);
      if (i === self.index) item.button.setAttribute("aria-current", "step");
      else item.button.removeAttribute("aria-current");
    });
    this.acts.forEach(function (a) {
      var open = a === currentAct;
      a.node.classList.toggle("is-open", open);
      a.node.classList.toggle("is-done", a.first + a.count - 1 < self.index);
    });
    if (this.captionEl) this.captionEl.textContent = step.comment;
    if (this.countEl) {
      this.countEl.textContent =
        (step.act ? step.act + " · " : "") + "step " + (this.index + 1) + " of " + this.steps.length;
    }
    var last = this.index === this.steps.length - 1;
    this.prevBtn.setAttribute("aria-disabled", this.index === 0 ? "true" : "false");
    this.nextBtn.setAttribute("aria-disabled", last ? "true" : "false");
    // On the last step the button starts the session over, so it says so.
    var label = this.playing ? "Pause" : this.finished || last ? "Replay" : "Play";
    this.toggleBtn.querySelector("[data-term-toggle-label]").textContent = label;
    this.root.classList.toggle("is-playing", this.playing);
  };

  Terminal.prototype.announce = function () {
    if (!this.statusEl) return;
    this.statusEl.textContent =
      "Step " + (this.index + 1) + " of " + this.steps.length + ": " + this.steps[this.index].comment;
  };

  /* ---- transport ------------------------------------------------------ */

  Terminal.prototype.stopTimers = function () {
    this.run += 1;
    if (this.timer) window.clearTimeout(this.timer);
    this.timer = null;
  };

  Terminal.prototype.pause = function () {
    if (!this.playing) return;
    this.stopTimers();
    this.playing = false;
    // Never leave a half-typed command on screen.
    this.renderThrough(this.index);
    this.sync();
  };

  Terminal.prototype.goTo = function (i, options) {
    var wasPlaying = this.playing;
    this.stopTimers();
    this.started = true;
    this.finished = false;
    this.autoPaused = false;
    if (wasPlaying && !REDUCED.matches) {
      this.renderThrough(i - 1);
      this.index = i;
      this.playStep();
    } else {
      this.playing = false;
      this.renderThrough(i);
    }
    this.sync();
    if (options && options.announce) this.announce();
  };

  Terminal.prototype.play = function () {
    if (REDUCED.matches) return;
    this.stopTimers();
    if (!this.started || this.finished || this.index >= this.steps.length - 1) {
      // First play, or a replay: type the session from the top.
      this.screen.textContent = "";
      this.index = 0;
    } else {
      // Continue with the step after the one already on screen.
      this.renderThrough(this.index);
      this.index += 1;
    }
    this.started = true;
    this.finished = false;
    this.playing = true;
    this.playStep();
  };

  Terminal.prototype.playStep = function () {
    var self = this;
    var run = this.run;
    var step = this.steps[this.index];
    var parts = this.entry(step);
    var cursor = el("span", "term__cursor");
    cursor.setAttribute("aria-hidden", "true");
    parts.line.appendChild(cursor);
    this.screen.appendChild(parts.entry);
    this.scrollToLatest();
    this.sync();

    var text = step.cmd;
    var perChar = Math.max(9, Math.min(30, 1300 / Math.max(text.length, 1)));
    var position = 0;

    function later(fn, ms) {
      self.timer = window.setTimeout(function () {
        if (run === self.run) fn();
      }, ms);
    }

    function type() {
      position += 1;
      parts.typed.textContent = text.slice(0, position);
      if (position < text.length) later(type, perChar);
      else later(reveal, 380);
    }

    function reveal() {
      cursor.remove();
      parts.entry.appendChild(self.output(step));
      self.scrollToLatest();
      var lines = (step.output || "").split("\n").length;
      later(advance, Math.min(4600, 1300 + lines * 150));
    }

    function advance() {
      if (self.index >= self.steps.length - 1) {
        self.playing = false;
        self.finished = true;
        self.sync();
        return;
      }
      self.index += 1;
      self.playStep();
    }

    later(type, 420);
  };

  /* ---- copy buttons --------------------------------------------------- */

  function wireCopyButtons() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (button) {
      var label = button.querySelector("[data-copy-label]");
      var status = button.parentElement.querySelector("[data-copy-status]");
      var idle = label ? label.textContent : "";
      var reset = null;
      button.addEventListener("click", function () {
        var text = button.getAttribute("data-copy");
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(text).then(function () {
          button.classList.add("is-copied");
          if (label) label.textContent = "Copied";
          if (status) status.textContent = "Install command copied";
          window.clearTimeout(reset);
          reset = window.setTimeout(function () {
            button.classList.remove("is-copied");
            if (label) label.textContent = idle;
            if (status) status.textContent = "";
          }, 1800);
        });
      });
    });
  }

  function start() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-term]"), function (root) {
      new Terminal(root).load();
    });
    wireCopyButtons();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
