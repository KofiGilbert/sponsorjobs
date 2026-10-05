/* A tiny QR code encoder (byte mode, error correction M, versions 1 to 10, up to 213 bytes).
   Enough for a Telegram link. No network, no dependency. Follows the structure of Project
   Nayuki's reference encoder (MIT). Usage: window.QR.svg("https://t.me/...") returns an SVG
   string; window.QR.matrix(text) returns rows of booleans. */
(function () {
  "use strict";
  // Index = version. Level M only.
  var ECC_PER_BLOCK = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26];
  var NUM_BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
  var FORMAT_M = 0;

  function rawModules(ver) {
    var r = (16 * ver + 128) * ver + 64;
    if (ver >= 2) {
      var na = Math.floor(ver / 7) + 2;
      r -= (25 * na - 10) * na - 55;
      if (ver >= 7) r -= 36;
    }
    return r;
  }
  function dataCodewords(ver) {
    return Math.floor(rawModules(ver) / 8) - ECC_PER_BLOCK[ver] * NUM_BLOCKS[ver];
  }
  function gfMul(x, y) {
    var z = 0;
    for (var i = 7; i >= 0; i--) {
      z = (z << 1) ^ ((z >>> 7) * 0x11d);
      z ^= ((y >>> i) & 1) * x;
    }
    return z;
  }
  function rsDivisor(degree) {
    var res = [];
    for (var i = 0; i < degree - 1; i++) res.push(0);
    res.push(1);
    var root = 1;
    for (i = 0; i < degree; i++) {
      for (var j = 0; j < res.length; j++) {
        res[j] = gfMul(res[j], root);
        if (j + 1 < res.length) res[j] ^= res[j + 1];
      }
      root = gfMul(root, 0x02);
    }
    return res;
  }
  function rsRemainder(data, div) {
    var res = div.map(function () { return 0; });
    data.forEach(function (b) {
      var f = b ^ res.shift();
      res.push(0);
      div.forEach(function (c, i) { res[i] ^= gfMul(c, f); });
    });
    return res;
  }
  function bit(x, i) { return ((x >>> i) & 1) !== 0; }

  function utf8(text) {
    if (typeof TextEncoder !== "undefined") return Array.from(new TextEncoder().encode(text));
    var s = unescape(encodeURIComponent(text)), out = [];
    for (var i = 0; i < s.length; i++) out.push(s.charCodeAt(i));
    return out;
  }

  function encode(text) {
    var bytes = utf8(text), ver = 0;
    for (var v = 1; v <= 10; v++) {
      var cc = v < 10 ? 8 : 16;
      if (4 + cc + bytes.length * 8 <= dataCodewords(v) * 8) { ver = v; break; }
    }
    if (!ver) throw new Error("QR: text too long");
    var bits = [];
    function push(val, len) { for (var i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); }
    push(4, 4);                                  // byte mode
    push(bytes.length, ver < 10 ? 8 : 16);
    bytes.forEach(function (b) { push(b, 8); });
    var cap = dataCodewords(ver) * 8;
    push(0, Math.min(4, cap - bits.length));
    push(0, (8 - bits.length % 8) % 8);
    for (var pad = 0xec; bits.length < cap; pad ^= 0xec ^ 0x11) push(pad, 8);
    var data = [];
    for (var k = 0; k < bits.length; k += 8) {
      var b = 0;
      for (var m = 0; m < 8; m++) b = (b << 1) | bits[k + m];
      data.push(b);
    }
    return build(ver, data);
  }

  function build(ver, data) {
    var size = ver * 4 + 17, mods = [], fn = [];
    for (var y = 0; y < size; y++) {
      mods.push(new Array(size).fill(false));
      fn.push(new Array(size).fill(false));
    }
    function setF(x, y, d) { mods[y][x] = d; fn[y][x] = true; }

    // timing, finders, alignment
    for (var i = 0; i < size; i++) { setF(6, i, i % 2 === 0); setF(i, 6, i % 2 === 0); }
    function finder(x, y) {
      for (var dy = -4; dy <= 4; dy++) for (var dx = -4; dx <= 4; dx++) {
        var d = Math.max(Math.abs(dx), Math.abs(dy)), xx = x + dx, yy = y + dy;
        if (xx >= 0 && xx < size && yy >= 0 && yy < size) setF(xx, yy, d !== 2 && d !== 4);
      }
    }
    finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
    var pos = [];
    if (ver > 1) {
      var na = Math.floor(ver / 7) + 2;
      var step = Math.ceil((ver * 4 + 4) / (na * 2 - 2)) * 2;
      pos = [6];
      for (var p = size - 7; pos.length < na; p -= step) pos.splice(1, 0, p);
    }
    for (var a = 0; a < pos.length; a++) for (var c = 0; c < pos.length; c++) {
      if ((a === 0 && c === 0) || (a === 0 && c === pos.length - 1) || (a === pos.length - 1 && c === 0)) continue;
      for (var dy2 = -2; dy2 <= 2; dy2++) for (var dx2 = -2; dx2 <= 2; dx2++)
        setF(pos[a] + dx2, pos[c] + dy2, Math.max(Math.abs(dx2), Math.abs(dy2)) !== 1);
    }
    function formatBits(mask) {
      var d = (FORMAT_M << 3) | mask, rem = d;
      for (var i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      var bits = ((d << 10) | rem) ^ 0x5412;
      for (i = 0; i <= 5; i++) setF(8, i, bit(bits, i));
      setF(8, 7, bit(bits, 6)); setF(8, 8, bit(bits, 7)); setF(7, 8, bit(bits, 8));
      for (i = 9; i < 15; i++) setF(14 - i, 8, bit(bits, i));
      for (i = 0; i < 8; i++) setF(size - 1 - i, 8, bit(bits, i));
      for (i = 8; i < 15; i++) setF(8, size - 15 + i, bit(bits, i));
      setF(8, size - 8, true);
    }
    formatBits(0);
    if (ver >= 7) {
      var rem = ver;
      for (i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      var vb = (ver << 12) | rem;
      for (i = 0; i < 18; i++) {
        var bb = bit(vb, i), aa = size - 11 + i % 3, cc = Math.floor(i / 3);
        setF(aa, cc, bb); setF(cc, aa, bb);
      }
    }

    // error correction + interleave
    var nb = NUM_BLOCKS[ver], el = ECC_PER_BLOCK[ver], raw = Math.floor(rawModules(ver) / 8);
    var nShort = nb - raw % nb, shortLen = Math.floor(raw / nb), div = rsDivisor(el), blocks = [];
    for (var bi = 0, k = 0; bi < nb; bi++) {
      var dat = data.slice(k, k + shortLen - el + (bi < nShort ? 0 : 1));
      k += dat.length;
      var ecc = rsRemainder(dat, div);
      if (bi < nShort) dat.push(0);
      blocks.push(dat.concat(ecc));
    }
    var all = [];
    for (i = 0; i < blocks[0].length; i++) for (var j = 0; j < blocks.length; j++)
      if (i !== shortLen - el || j >= nShort) all.push(blocks[j][i]);

    // place codewords
    var n = 0;
    for (var right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (var vert = 0; vert < size; vert++) for (var jj = 0; jj < 2; jj++) {
        var x = right - jj, up = ((right + 1) & 2) === 0, yy2 = up ? size - 1 - vert : vert;
        if (!fn[yy2][x] && n < all.length * 8) { mods[yy2][x] = bit(all[n >>> 3], 7 - (n & 7)); n++; }
      }
    }

    function maskHit(m, x, y) {
      switch (m) {
        case 0: return (x + y) % 2 === 0;
        case 1: return y % 2 === 0;
        case 2: return x % 3 === 0;
        case 3: return (x + y) % 3 === 0;
        case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
        case 5: return (x * y) % 2 + (x * y) % 3 === 0;
        case 6: return ((x * y) % 2 + (x * y) % 3) % 2 === 0;
        default: return ((x + y) % 2 + (x * y) % 3) % 2 === 0;
      }
    }
    function applyMask(m) {
      for (var y = 0; y < size; y++) for (var x = 0; x < size; x++)
        if (!fn[y][x] && maskHit(m, x, y)) mods[y][x] = !mods[y][x];
    }
    function penalty() {
      var s = 0, dark = 0, y, x;
      for (y = 0; y < size; y++) {              // runs of 5+ in rows and columns
        var rr = 1, rc = 1;
        for (x = 1; x < size; x++) {
          if (mods[y][x] === mods[y][x - 1]) { rr++; if (rr === 5) s += 3; else if (rr > 5) s++; } else rr = 1;
          if (mods[x][y] === mods[x - 1][y]) { rc++; if (rc === 5) s += 3; else if (rc > 5) s++; } else rc = 1;
        }
      }
      for (y = 0; y < size - 1; y++) for (x = 0; x < size - 1; x++) {   // 2x2 blocks
        var v = mods[y][x];
        if (v === mods[y][x + 1] && v === mods[y + 1][x] && v === mods[y + 1][x + 1]) s += 3;
      }
      for (y = 0; y < size; y++) for (x = 0; x < size; x++) if (mods[y][x]) dark++;
      var total = size * size;
      s += (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
      return s;
    }
    var best = 0, bestScore = Infinity;
    for (var mk = 0; mk < 8; mk++) {
      applyMask(mk); formatBits(mk);
      var sc = penalty();
      if (sc < bestScore) { bestScore = sc; best = mk; }
      applyMask(mk);                             // XOR again to undo
    }
    applyMask(best); formatBits(best);
    return mods;
  }

  function svg(text, opts) {
    var m = encode(text), size = m.length, q = 4, dim = size + q * 2, path = "";
    for (var y = 0; y < size; y++) for (var x = 0; x < size; x++)
      if (m[y][x]) path += "M" + (x + q) + "," + (y + q) + "h1v1h-1z";
    var px = (opts && opts.px) || 168;
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + dim + " " + dim + '" width="' + px +
      '" height="' + px + '" shape-rendering="crispEdges" role="img" aria-label="QR code">' +
      '<rect width="100%" height="100%" fill="#fff"/><path d="' + path + '" fill="#000"/></svg>';
  }

  var api = { matrix: encode, svg: svg, _rsDivisor: rsDivisor, _rsRemainder: rsRemainder };
  if (typeof window !== "undefined") window.QR = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
