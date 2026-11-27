/* The celebration burst (2026-11-27, the owner's idea, like a merged pull request's confetti).
   Plain canvas, no library. Two sizes: "light" (a routine submit) and "big" (the first application,
   every 10th, an interview, an offer). Brand violet with a few warm accents; it falls away by
   itself in about 1.4 s. The page is shown only when motion is allowed (the app checks first). */
(() => {
  "use strict";
  const cv = document.getElementById("c");
  if (!cv || !cv.getContext) return;
  const big = /big/.test(location.hash);
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const W = window.innerWidth, H = window.innerHeight;
  cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
  const g = cv.getContext("2d");
  g.scale(dpr, dpr);
  const COLORS = ["#7957FC", "#7957FC", "#9B82FD", "#B9A8FF", "#5B3BE6", "#FFB547", "#FF7A59", "#FFD166"];
  const N = big ? 170 : 70;
  const LIFE = 1400;
  const parts = [];
  // from the bottom corners for a big one (two cannons), from the top middle for a light one
  for (let i = 0; i < N; i++) {
    let x, y, vx, vy;
    if (big) {
      const left = i % 2 === 0;
      x = left ? W * 0.08 : W * 0.92; y = H * 0.95;
      const a = (left ? -60 : -120) * Math.PI / 180 + (Math.random() - 0.5) * 0.7;
      const sp = (0.9 + Math.random() * 0.7) * Math.min(H, 900) * 0.0021;
      vx = Math.cos(a) * sp; vy = Math.sin(a) * sp;
    } else {
      x = W / 2 + (Math.random() - 0.5) * W * 0.5; y = -10 - Math.random() * 40;
      vx = (Math.random() - 0.5) * 0.35; vy = 0.15 + Math.random() * 0.35;
    }
    parts.push({ x, y, vx, vy, w: 6 + Math.random() * 6, h: 3 + Math.random() * 4, r: Math.random() * Math.PI,
                 vr: (Math.random() - 0.5) * 0.02, c: COLORS[i % COLORS.length], ribbon: Math.random() < 0.25 });
  }
  const t0 = performance.now();
  let last = t0;
  function frame(now) {
    const dt = Math.min(40, now - last); last = now;
    const age = now - t0;
    g.clearRect(0, 0, W, H);
    const fade = age > LIFE - 400 ? Math.max(0, (LIFE - age) / 400) : 1;
    for (const p of parts) {
      p.vy += 0.0009 * dt; p.vx *= 0.995; p.vy *= 0.995;
      p.x += p.vx * dt; p.y += p.vy * dt; p.r += p.vr * dt;
      g.save();
      g.globalAlpha = fade;
      g.translate(p.x, p.y); g.rotate(p.r);
      g.fillStyle = p.c;
      if (p.ribbon) { g.fillRect(-p.w, -1, p.w * 2, 2.2); }
      else { g.scale(1, Math.abs(Math.cos(p.r * 2)) + 0.15); g.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); }
      g.restore();
    }
    if (age < LIFE) requestAnimationFrame(frame);
    else g.clearRect(0, 0, W, H);
  }
  requestAnimationFrame(frame);
})();
