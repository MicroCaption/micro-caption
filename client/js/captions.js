// captions.js — shared caption pagination + reading-paced display.
//
// The server now emits an append-only stream of small committed fragments
// (a few words each, via LocalAgreement-2). This module stitches those
// fragments into ≤2-line blocks and releases them at a comfortable reading
// pace, holding each block on screen long enough to actually read — while
// never falling more than ~MAX_LAG seconds behind live (it shortens dwell and,
// when badly backed up, skips ahead).
//
// Exposes two globals (loaded as a classic script, before player.js/watch.js):
//   wrapLines(text, maxChars, maxLines) → string[]   (line-wrap helper)
//   CaptionPacer                                       (the paced display queue)

(function (global) {
  'use strict';

  const DEFAULTS = {
    maxChars: 32,        // chars per line (CEA-608 safe-title width)
    maxLines: 2,         // visible rows
    readingCps: 14,      // reading speed ≈ 168 wpm (≈5 chars/word)
    minDwell: 1200,      // ms — floor so nothing flashes by
    maxDwell: 4000,      // ms — ceiling so the screen doesn't stall
    // maxLagSec is the main "closeness to live" knob: the pacer speeds up
    // (shortens dwell, then drops backlog) whenever it falls this far behind.
    // Lower = closer to live but text turns over faster; higher = calmer reading.
    maxLagSec: 2.0,      // target lag behind live
    flushGapMs: 500,     // flush a partial block after this much silence
    idleClearMs: 6000,   // clear the screen after this long with nothing new
    maxBacklog: 3,       // queued blocks beyond this get dropped to catch up
  };

  // Wrap text into lines of ≤ maxChars, keeping the last maxLines lines.
  function wrapLines(text, maxChars, maxLines) {
    maxChars = maxChars || DEFAULTS.maxChars;
    maxLines = maxLines || DEFAULTS.maxLines;
    const words = String(text).trim().split(/\s+/);
    const lines = [];
    let line = '';
    for (const w of words) {
      if (!w) continue;
      const cand = line ? line + ' ' + w : w;
      if (cand.length <= maxChars) {
        line = cand;
      } else {
        if (line) lines.push(line);
        line = w;
      }
    }
    if (line) lines.push(line);
    return maxLines ? lines.slice(-maxLines) : lines;
  }

  const SENTENCE_END = /[.!?…]["'')\]]?$/;

  // A paced caption display fed by push(text, start, end).
  // render(lines) is called whenever the on-screen text should change.
  function CaptionPacer(render, opts) {
    this.render = render;
    this.opt = Object.assign({}, DEFAULTS, opts || {});

    this._cur = [];          // words accumulating into the current block
    this._queue = [];        // ready blocks awaiting display (FIFO)
    this._showing = null;    // block currently on screen
    this._latestEnd = 0;     // newest fragment end time (stream seconds) ≈ "live"
    this._paused = false;

    this._advanceTimer = null;
    this._flushTimer = null;
    this._idleTimer = null;
  }

  CaptionPacer.prototype.push = function (text, start, end) {
    const clean = String(text || '').replace(/\s+/g, ' ').trim();
    if (!clean) return;
    if (typeof end === 'number' && !isNaN(end)) {
      this._latestEnd = Math.max(this._latestEnd, end);
    }
    if (this._idleTimer) { clearTimeout(this._idleTimer); this._idleTimer = null; }

    const words = clean.split(' ');
    for (const w of words) {
      const tentative = this._cur.concat(w);
      if (wrapLines(tentative.join(' '), this.opt.maxChars, 0).length > this.opt.maxLines) {
        // Adding this word would overflow 2 lines — seal the current block.
        this._seal();
        this._cur = [w];
      } else {
        this._cur = tentative;
        // Break on sentence boundaries (once the block has some substance).
        if (SENTENCE_END.test(w) && this._cur.join(' ').length >= this.opt.maxChars) {
          this._seal();
        }
      }
    }

    if (this._flushTimer) clearTimeout(this._flushTimer);
    this._flushTimer = setTimeout(() => { this._flushTimer = null; this._seal(); this._kick(); },
                                  this.opt.flushGapMs);
    this._kick();
  };

  // Move the accumulating words into a ready block.
  CaptionPacer.prototype._seal = function () {
    if (!this._cur.length) return;
    const text = this._cur.join(' ');
    this._queue.push({
      lines: wrapLines(text, this.opt.maxChars, this.opt.maxLines),
      chars: text.length,
      endTime: this._latestEnd,
    });
    this._cur = [];
  };

  // Show the next block if nothing is currently displaying.
  CaptionPacer.prototype._kick = function () {
    if (this._paused || this._showing || !this._queue.length) return;
    this._advance();
  };

  CaptionPacer.prototype._advance = function () {
    // Catch-up: if we're badly backed up, drop the oldest blocks so we never
    // sit more than ~maxLag behind live.
    while (this._queue.length > this.opt.maxBacklog) this._queue.shift();

    const block = this._queue.shift();
    if (!block) return;
    this._showing = block;
    this.render(block.lines);

    // Base dwell from reading speed, shortened when we're behind live.
    let dwell = (block.chars / this.opt.readingCps) * 1000;
    dwell = Math.max(this.opt.minDwell, Math.min(this.opt.maxDwell, dwell));
    const lag = this._latestEnd - block.endTime;
    if (lag > this.opt.maxLagSec || this._queue.length > 1) {
      dwell = this.opt.minDwell;
    }

    this._advanceTimer = setTimeout(() => this._onDwellEnd(), dwell);
  };

  CaptionPacer.prototype._onDwellEnd = function () {
    this._advanceTimer = null;
    this._showing = null;
    if (this._queue.length) {
      this._advance();
    } else {
      // Nothing queued — leave the last block up briefly, then clear.
      this._idleTimer = setTimeout(() => {
        this._idleTimer = null;
        this.render([]);
      }, this.opt.idleClearMs);
    }
  };

  CaptionPacer.prototype.pause = function () {
    this._paused = true;
    if (this._advanceTimer) { clearTimeout(this._advanceTimer); this._advanceTimer = null; }
    if (this._idleTimer) { clearTimeout(this._idleTimer); this._idleTimer = null; }
  };

  CaptionPacer.prototype.resume = function () {
    if (!this._paused) return;
    this._paused = false;
    if (this._showing) {
      // A block was frozen mid-dwell (pause cleared its advance timer) —
      // restart advancement so we don't get stuck on it.
      if (!this._advanceTimer) {
        this._advanceTimer = setTimeout(() => this._onDwellEnd(), this.opt.minDwell);
      }
    } else {
      this._kick();
    }
  };

  CaptionPacer.prototype.reset = function () {
    if (this._advanceTimer) clearTimeout(this._advanceTimer);
    if (this._flushTimer) clearTimeout(this._flushTimer);
    if (this._idleTimer) clearTimeout(this._idleTimer);
    this._advanceTimer = this._flushTimer = this._idleTimer = null;
    this._cur = [];
    this._queue = [];
    this._showing = null;
    this.render([]);
  };

  global.wrapLines = wrapLines;
  global.CaptionPacer = CaptionPacer;
})(window);
