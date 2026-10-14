"use strict";
  // A single-page app over three steps, like Workday / SmartRecruiters: pressing Next swaps the
  // fields without changing the URL. ?wall=1 puts a create-account step in as page 2.
  const W = window.FixtureWidgets, step = document.getElementById("step");
  const wall = new URLSearchParams(location.search).get("wall") === "1";
  window.fx = {};
  const pages = [
    () => { step.innerHTML = '<h2>Step 1: Contact</h2>'
      + '<div class="field"><label for="fn">First name</label><input type="text" id="fn"></div>'
      + '<div class="field"><label for="ln">Last name</label><input type="text" id="ln"></div>'
      + '<div class="field"><label for="em">Email</label><input type="email" id="em"></div>'
      + '<div class="field"><label for="ph">Phone</label><input type="tel" id="ph"></div>'; },
    () => { step.innerHTML = '<h2>Step 2: Questions</h2><div id="w"></div>'
      + '<div class="field"><label for="yrs">How many years of experience do you have?</label><input type="text" id="yrs"></div>'
      + '<div class="field" id="cond"></div>';
      const host = document.getElementById("w");
      window.fx.auth = W.ReactSelect(host, { id: "q-auth", label: "Are you legally authorized to work in the United States?",
        options: ["Yes", "No"],
        onChange: () => {         // a conditional follow-up on the SAME page
          document.getElementById("cond").innerHTML = '<label for="cnote">Anything else we should know?</label><input type="text" id="cnote">'; } });
      window.fx.sponsor = W.AriaRadioGroup(host, { label: "Will you now or in the future require visa sponsorship?", options: ["Yes", "No"] }); },
    () => { step.innerHTML = '<h2>Step 3: Voluntary disclosures</h2><div id="w"></div>'
      + '<div class="field"><label for="vet">Veteran status</label><select id="vet"><option value="">Select...</option>'
      + '<option>I am a protected veteran</option><option>I am not a protected veteran</option>'
      + '<option>I do not wish to self-identify</option></select></div>'
      + '<button type="button" id="submit">Submit application</button>';
      window.fx.gender = W.ReactSelect(document.getElementById("w"), { id: "q-gender", label: "Gender",
        options: ["Female", "Male", "I don't wish to answer"] }); },
  ];
  if (wall) pages.splice(1, 0, () => { step.innerHTML = '<h2>Create your account to continue</h2>'
      + '<div class="field"><label for="ae">Email</label><input type="email" id="ae"></div>'
      + '<div class="field"><label for="pw">Password</label><input type="password" id="pw"></div>'
      + '<p>Please sign in to continue your application.</p>'; });
  let i = 0;
  pages[0]();
  document.getElementById("next").addEventListener("click", () => { if (i < pages.length - 1) pages[++i](); });
  window.fxPage = () => i + 1;
