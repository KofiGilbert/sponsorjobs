"use strict";
  // A whole mock application system in one single-page app, for the apply agent's end-to-end
  // run (docs/apply-agent.md, milestone 1): a job page, an account wall, three form pages, a
  // review page, and the "application received" page, with no URL change between them (like
  // Workday). ?captcha=1 puts a bot check on the review page so the run must stop there.
  // ?signin=fail makes the first Sign in attempt fail so the agent has to create the account.
  const W = window.FixtureWidgets, step = document.getElementById("step");
  const q = new URLSearchParams(location.search);
  const captcha = q.get("captcha") === "1";
  const signinFails = q.get("signin") === "fail";
  const verify = q.get("verify") === "1";             // a 6-digit code is emailed after sign-in
  const resetToken = q.get("reset") || "";             // the emailed reset link lands here
  const mustAnswer = q.get("mustanswer") === "1";      // years of experience is required
  // ?phenom=1: Lam Research's Phenom form (2026-10-30). Attaching the resume opens a "Data
  // Privacy Agreement" dialog that must be acknowledged, and the disclosures are Phenom's
  // dropdowns (ignore Escape and blur, list over the next field), all four required.
  const phenom = q.get("phenom") === "1";
  // ?address=1: the contact page also asks City, State and ZIP code (ZIP required), filled from
  // the applicant brain's memory (docs/applicant-brain.md, Phase 3).
  const address = q.get("address") === "1";
  const CODE = "482913";
  const log = [];                                      // what happened, read by the test
  window.fxLog = () => log.slice();
  window.fx = {};
  let signinTries = 0;

  const page = (html) => { step.innerHTML = html; };
  const btn = (id, text) => `<button type="button" id="${id}">${text}</button>`;
  const pages = {
    job() {
      page('<p class="eyebrow">Acme Corp</p><h2>Business Intelligence Analyst</h2>'
        + '<p>Bloomington, IL. Full time. Posted 14 days ago.</p>'
        + '<p>We are looking for an analyst who turns data into decisions.</p>'
        + btn("apply", "Apply"));
      $("apply").onclick = () => { log.push("apply"); pages.account(); };
    },
    account() {
      page('<h2>Sign in to continue</h2>'
        + '<div class="field"><label for="ae">Email Address</label><input type="email" id="ae"></div>'
        + '<div class="field"><label for="pw">Password</label><input type="password" id="pw"></div>'
        + '<p id="err" class="error" hidden></p>'
        + btn("signin", "Sign in") + ' ' + btn("create", "Create account") + ' <a href="#forgot" id="forgot">Forgot password?</a>');
      $("signin").onclick = () => {
        signinTries++;
        if (!$("ae").value || !$("pw").value) { $("err").textContent = "Email and password are required."; $("err").hidden = false; return; }
        if (signinFails && signinTries === 1) { $("err").textContent = "We could not find an account with that email."; $("err").hidden = false; log.push("signin-failed"); return; }
        log.push("signin");
        if (verify) pages.code(); else pages.contact();
      };
      $("create").onclick = () => { log.push("create-account-page"); pages.create(); };
      $("forgot").onclick = () => { log.push("forgot"); pages.reset(); };
    },
    code() {
      page('<h2>Check your email</h2><p>Enter the 6-digit code we sent to your email address.</p>'
        + '<div class="field"><label for="otp">Verification code</label><input type="text" id="otp" inputmode="numeric"></div>'
        + '<p id="err" class="error" hidden></p>' + btn("verify", "Verify"));
      $("verify").onclick = () => {
        if ($("otp").value.trim() !== CODE) { $("err").textContent = "That code is not right."; $("err").hidden = false; log.push("code-wrong"); return; }
        log.push("code-ok"); pages.contact();
      };
    },
    reset() {
      page('<h2>Reset your password</h2><p>Enter your email and we will send you a link to reset it.</p>'
        + '<div class="field"><label for="re">Email Address</label><input type="email" id="re"></div>'
        + btn("sendreset", "Send reset link"));
      $("sendreset").onclick = () => {
        if (!$("re").value) return;
        log.push("reset-requested");
        page('<h2>Check your email</h2><p>We emailed you a link to reset your password. Click the link in the email to continue.</p>');
      };
    },
    newpw() {
      page('<h2>Choose a new password</h2>'
        + '<div class="field"><label for="np">New Password</label><input type="password" id="np"></div>'
        + '<div class="field"><label for="np2">Confirm New Password</label><input type="password" id="np2"></div>'
        + '<p id="err" class="error" hidden></p>' + btn("savepw", "Save password"));
      $("savepw").onclick = () => {
        if (!$("np").value || $("np").value !== $("np2").value) { $("err").textContent = "Passwords do not match."; $("err").hidden = false; return; }
        log.push("password-reset"); pages.contact();
      };
    },
    create() {
      page('<h2>Create your account</h2>'
        + '<div class="field"><label for="ce">Email Address</label><input type="email" id="ce"></div>'
        + '<div class="field"><label for="cp">Create Password</label><input type="password" id="cp"></div>'
        + '<div class="field"><label for="cp2">Verify New Password</label><input type="password" id="cp2"></div>'
        + '<p id="err" class="error" hidden></p>' + btn("createbtn", "Create account"));
      $("createbtn").onclick = () => {
        if (!$("ce").value || !$("cp").value) { $("err").textContent = "All fields are required."; $("err").hidden = false; return; }
        if ($("cp").value !== $("cp2").value) { $("err").textContent = "Passwords do not match."; $("err").hidden = false; return; }
        log.push("account-created"); pages.contact();
      };
    },
    contact() {
      page('<h2>My Information</h2><p class="muted">Step 1 of 3</p>'
        + '<div class="field"><label for="fn">First name</label><input type="text" id="fn"></div>'
        + '<div class="field"><label for="ln">Last name</label><input type="text" id="ln"></div>'
        + '<div class="field"><label for="em">Email</label><input type="email" id="em"></div>'
        + '<div class="field"><label for="ph">Phone</label><input type="tel" id="ph"></div>'
        + (address ? '<div class="field"><label for="city">City</label><input type="text" id="city"></div>'
            + '<div class="field"><label for="st">State</label><input type="text" id="st"></div>'
            + '<div class="field"><label for="zip">ZIP code</label><input type="text" id="zip" required></div>'
            + '<p id="err" class="error" hidden></p>' : '')
        + '<div class="field"><label for="cv">Resume/CV</label><input type="file" id="cv" style="opacity:0;position:absolute;width:1px;height:1px"><button type="button" id="cvbtn">Upload</button><span id="cvname"></span></div>'
        + btn("next", "Save and Continue"));
      $("cvbtn").onclick = () => $("cv").click();
      let dlg = null;
      if (phenom) {
        dlg = W.PrivacyDialog({ company: "Acme Corp", onAck: () => log.push("privacy-ack"),
                                onCancel: () => { log.push("privacy-cancel"); $("cv").value = ""; $("cvname").textContent = ""; } });
      }
      $("cv").onchange = () => {
        $("cvname").textContent = ($("cv").files[0] || {}).name || "";
        if (dlg && ($("cv").files || []).length) { log.push("privacy-dialog"); dlg.open(); }
      };
      $("next").onclick = () => {
        if (dlg && ($("cv").files || []).length && !dlg.acked) { log.push("privacy-needed"); return; }
        if (address) {
          if (!$("zip").value.trim()) { $("err").textContent = "The field ZIP code is required and must have a value."; $("err").hidden = false; log.push("zip-required"); return; }
          log.push("address:" + ["city", "st", "zip"].map((i) => $(i).value.trim()).join("|"));
        }
        log.push("contact:" + ["fn", "ln", "em", "ph"].filter((i) => $(i).value).length + ":" + (($("cv").files || []).length ? "cv" : "nocv"));
        pages.questions();
      };
    },
    questions() {
      page('<h2>Application Questions</h2><p class="muted">Step 2 of 3</p><div id="w"></div>'
        + '<div class="field"><label for="yrs">How many years of experience do you have?</label><input type="text" id="yrs"' + (mustAnswer ? ' required' : '') + '></div>'
        + '<p id="err" class="error" hidden></p>'
        + btn("back", "Back") + ' ' + btn("next", "Save and Continue"));
      const host = $("w");
      window.fx.auth = W.ReactSelect(host, { id: "q-auth", label: "Are you legally authorized to work in the United States?", options: ["Yes", "No"] });
      window.fx.sponsor = W.AriaRadioGroup(host, { label: "Will you now or in the future require visa sponsorship?", options: ["Yes", "No"] });
      $("back").onclick = () => pages.contact();
      $("next").onclick = () => {
        if (mustAnswer && !$("yrs").value.trim()) { $("err").textContent = "The field How many years of experience do you have? is required and must have a value."; $("err").hidden = false; log.push("years-required"); return; }
        log.push("questions:" + ($("yrs").value.trim() || "-")); pages.disclosures();
      };
    },
    disclosures() {
      if (phenom) return pages.phenomDisclosures();
      page('<h2>Voluntary Disclosures</h2><p class="muted">Step 3 of 3</p>'
        + '<div class="field"><label for="vet">Veteran status</label><select id="vet"><option value="">Select...</option>'
        + '<option>I am a protected veteran</option><option>I am not a protected veteran</option><option>I do not wish to self-identify</option></select></div>'
        + '<div class="field"><label><input type="checkbox" id="agree"> I agree to the terms and conditions</label></div>'
        + btn("next", "Save and Continue"));
      $("next").onclick = () => { log.push("disclosures"); pages.review(); };
    },
    phenomDisclosures() {
      page('<h2>Job-Specific Information</h2><p class="muted">Step 3 of 3</p><div id="w"></div>'
        + '<p id="err" class="error" hidden></p>' + btn("next", "Save and Continue"));
      const host = $("w");
      const f = {
        gender: W.PhenomSelect(host, { id: "q_custGender", label: "Gender", options: ["Choose not to disclose", "Male", "Female"] }),
        ethnicity: W.PhenomSelect(host, { id: "q_ethnicity", label: "Ethnicity", options: ["American Indian/Alaskan Native", "Asian",
          "Black or African American", "Choose not to disclose", "Hispanic or Latino", "Native Hawaiian or Other Pacific Island",
          "Two or More Races", "White"] }),
        disability: W.PhenomSelect(host, { id: "disability", label: "Disability", options: ["Choose not to disclose", "No", "Yes"] }),
        veteran: W.PhenomSelect(host, { id: "veteran", label: "Veteran", options: ["Choose not to disclose", "No", "Yes"],
          note: "Veteran To meet U.S. Department of Labor requirements, federal contractors report annually the number of employees who are: 1) special disabled veterans 2) veterans of the Vietnam era and 3) other protected veterans." }),
      };
      window.fx.eeo = f;
      $("next").onclick = () => {
        const open = Object.entries(f).filter(([, w]) => !w.value).map(([k]) => k);
        if (open.length) { $("err").textContent = open.map((k) => "The field " + k[0].toUpperCase() + k.slice(1) + " is required and must have a value.").join(" "); $("err").hidden = false; log.push("eeo-open:" + open.join(",")); return; }
        log.push("eeo:" + Object.keys(f).length); pages.review();
      };
    },
    review() {
      page('<h2>Review</h2><p>Check your application, then submit it.</p>'
        + '<dl><dt>Name</dt><dd id="rv-name"></dd><dt>Email</dt><dd id="rv-email"></dd></dl>'
        + (captcha ? '<div class="captcha"><label><input type="checkbox" id="robot"> I\'m not a robot</label></div>' : '')
        + btn("submit", "Submit application"));
      $("submit").onclick = () => {
        if (captcha && !$("robot").checked) { log.push("captcha-blocked"); return; }
        log.push("submitted"); pages.done();
      };
    },
    done() {
      page('<h2>Thank you</h2><p>Your application was received. We will be in touch.</p>');
    },
  };
  function $(id) { return document.getElementById(id); }
  window.fxGo = (name) => { if (pages[name]) pages[name](); };      // tests jump to a page
  if (resetToken) { log.push("reset-link-opened"); pages.newpw(); } else pages.job();
