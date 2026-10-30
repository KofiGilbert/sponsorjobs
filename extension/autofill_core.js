// The assisted-apply FILL ENGINE, shared by two carriers:
//   1. the browser extension (autofill.js adds the panel UI and talks to the app
//      through the background worker), loaded before autofill.js by the manifest;
//   2. the desktop shell (shell-electron injects this into its in-app browser pane
//      and calls TailorAutofill.fill(payload) directly).
// It only ever PRE-FILLS: empty fields, saved Yes/No screening answers, and the
// neutral EEO "decline" the person opted into. It never clicks submit, never
// uploads a file, never guesses free text (CLAUDE.md §7, assisted apply).
(function () {
  "use strict";
  if (window.TailorAutofill) return;   // one engine per page, whoever injects first

  // ------------------------------------------------------------------ fill recipes
  // Each application system is DATA (autoapply/recipes.json, served by the app at
  // GET /api/autofill/recipes): URL patterns, ordered selectors per profile key, the
  // continue/next and submit buttons, and the "application received" markers. The copy
  // below is the OFFLINE FALLBACK, regenerated from recipes.json by scripts/embed_recipes.py
  // (a test keeps them identical). The order of play on a page is: the recipe for the
  // current URL, then the label-pattern categories below, then (shell only) the model plan
  // for whatever is still empty. A recipe only ever PRE-FILLS and HIGHLIGHTS: continue is a
  // hint, submit is outlined for the person, and nothing is clicked (CLAUDE.md section 7).
  const RECIPES_FALLBACK = /*RECIPES_JSON_START*/{"version":1,"updated":"2026-10-11","about":"Fill recipes for assisted apply (CLAUDE.md section 7). Each system is DATA: URL patterns, ordered CSS selectors per profile key, the continue/next buttons (shown as a hint, never clicked), the submit buttons (HIGHLIGHTED for the person, never clicked), the application-received markers, and notes. Served at GET /api/autofill/recipes and embedded in extension/autofill_core.js as the offline fallback copy (scripts/embed_recipes.py keeps them in sync). Verified by fetching each system's live public page with curl on the date in each entry's 'verified' block; where a system renders its form only with JavaScript the raw HTML is recorded as seen and selectors come from the vendor's own shipped bundle or documented attributes, never guessed.","profile_keys":["first_name","last_name","name","email","phone","address","street","city","state","zip","country","location","linkedin","github","website","resume"],"question_patterns":{"work_auth_us":{"label_re":"authoriz(ed|ation) to work|legally (able|authorized|permitted|eligible) to work|eligible to work|right to work|work authoriz|lawfully (able|authorized)","answer_from":"work_auth_us","answers":["Yes","No"]},"needs_sponsorship":{"label_re":"sponsor|require.*visa|visa.*(status|sponsor|support)|employment visa|immigration (support|sponsor)","future_re":"now or in the future|in the future|will you (ever|at any time)|future","answer_from":"needs_sponsorship","answers":["Yes","No","Future"]},"eeo_decline":{"label_re":"gender|\\brace\\b|ethnic|veteran|disab|sexual orientation|pronoun","option_re":"decline|prefer not|not to (answer|identify|disclose|say)|don'?t (wish|want)|do not (wish|want)|not declared","answer_from":"eeo_decline"}},"systems":[{"id":"greenhouse","name":"Greenhouse","url_patterns":["https://job-boards.greenhouse.io/*","https://boards.greenhouse.io/*","https://job-boards.eu.greenhouse.io/*","https://boards.eu.greenhouse.io/*"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML","urls":["https://job-boards.greenhouse.io/anthropic/jobs/5183044008","https://boards.greenhouse.io/embed/job_app?for=anthropic&token=5183044008"]},"form":"form#application-form","fields":{"first_name":["#first_name","input[autocomplete='given-name']"],"last_name":["#last_name","input[autocomplete='family-name']"],"email":["#email","input[autocomplete='email']"],"phone":["#phone","input[type='tel']"],"country":["#country"],"location":["#candidate-location","#job_application_location","#auto_complete_input"],"linkedin":["input[aria-label*='LinkedIn' i]"],"website":["input[aria-label*='Website' i]","input[aria-label*='Portfolio' i]"],"resume":["#resume","input[type='file'][accept*='.pdf']"]},"continue_buttons":[],"submit_buttons":["form#application-form button[type='submit']","#submit_app","input[type='submit'][value*='Submit' i]"],"success_markers":[{"text":"Thank you for applying"},{"selector":"#application_confirmation"},{"text":"Your application has been submitted"}],"multipage":false,"notes":"boards.greenhouse.io now redirects to job-boards.greenhouse.io; both serve the same server-rendered form (#application-form with #first_name, #last_name, #email, #phone, #country react-select combobox, #resume file input, custom questions as #question_<id>). Submit is 'Submit application' (button[type=submit]). Custom and EEO questions are react-select comboboxes keyed by question id, so they fall through to the label matcher. Captcha-free on the pages read; some boards add reCAPTCHA at submit, which is the person's to solve. Re-read 2026-10-11: #first_name, #last_name, #email, #phone (type=tel), #resume (file), #country (react-select input) and the single button[type=submit] are present; this board has no LinkedIn/website input, where a board asks for them they are custom questions (#question_<id>) the label matcher reads from their <label for>."},{"id":"lever","name":"Lever","url_patterns":["https://jobs.lever.co/*","https://jobs.eu.lever.co/*"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML","urls":["https://jobs.lever.co/abovelending/0adfabd0-e18b-495e-add5-52a2521043dd/apply"]},"form":"form#application-form","fields":{"name":["input[name='name']","input[data-qa='name-input']"],"email":["input[name='email']","input[data-qa='email-input']"],"phone":["input[name='phone']","input[data-qa='phone-input']"],"location":["#location-input","input[name='location']","input[data-qa='location-input']"],"linkedin":["input[name='urls[LinkedIn]']"],"github":["input[name='urls[GitHub]']"],"website":["input[name='urls[Website]']","input[name='urls[Portfolio]']"],"resume":["#resume-upload-input","input[data-qa='input-resume']","input[name='resume']"]},"continue_buttons":[],"submit_buttons":["#btn-submit","button[data-qa='btn-submit']"],"success_markers":[{"url_re":"/thanks(\\?|$)"},{"text":"Application submitted"},{"text":"Thank you for your interest"}],"multipage":false,"notes":"Server-rendered single form (form#application-form). Fields carry data-qa hooks (name-input, email-input, phone-input, location-input, org-input, input-resume, btn-submit). Current company is input[name='org']. Custom questions are cards[<uuid>][field<n>] radios/selects, answered by the label matcher. hCaptcha is on the form (#hcaptchaSubmitBtn, hcaptchaResponse): the submit click is always the person's. After submit Lever navigates to .../thanks."},{"id":"ashby","name":"Ashby","url_patterns":["https://jobs.ashbyhq.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell only: <div id='root'>, Vite bundle); field paths from Ashby's Posting API application-form schema (_systemfield_*)","urls":["https://jobs.ashbyhq.com/assured-health/f373ebcc-def6-47e3-94cc-882e48bf7d2c/application"]},"form":"form","fields":{"name":["#_systemfield_name","input[name='_systemfield_name']"],"email":["#_systemfield_email","input[name='_systemfield_email']"],"phone":["#_systemfield_phone","input[name='_systemfield_phone']"],"location":["#_systemfield_location","input[name='_systemfield_location']"],"linkedin":["input[name*='linkedin' i]","input[id*='linkedin' i]"],"resume":["#_systemfield_resume","input[name='_systemfield_resume']","input[type='file']"]},"continue_buttons":[],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"Application submitted"},{"text":"Your application has been received"}],"multipage":false,"notes":"The raw HTML is an empty React root; the application form renders from the Posting API's application-form definition, whose system fields are keyed _systemfield_name, _systemfield_email, _systemfield_phone, _systemfield_location, _systemfield_resume. The DOM attribute placement (id/name) is not visible to curl, so every field also falls through to the label matcher. The 'Submit application' button is the only type=submit button on the page."},{"id":"workable","name":"Workable","url_patterns":["https://apply.workable.com/*","https://*.workable.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell, rspack bundle) + the live candidate bundle's form configuration (ids firstname, lastname, email, phone, headline, summary, resume, cover_letter, address, photo) and data-ui hooks","urls":["https://apply.workable.com/tiger-analytics/j/39D0D71D6D/apply","https://dcvxs6ggqztsa.cloudfront.net/candidate/releases/careers.9bfb2ee0b8c70361.js"]},"form":"form","fields":{"first_name":["input[name='firstname']","#firstname","[data-ui='firstname'] input"],"last_name":["input[name='lastname']","#lastname","[data-ui='lastname'] input"],"email":["input[name='email']","#email","[data-ui='email'] input"],"phone":["input[name='phone']","#phone","[data-ui='phone'] input"],"address":["input[name='address']","#address","[data-ui='address'] input"],"resume":["[data-ui='resume'] input[type='file']","input[name='resume']","input[type='file']"]},"continue_buttons":[],"submit_buttons":["button[data-ui='submit-application']","button[data-ui='submit-eeoc']","button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"selector":"[data-ui='eeoc-form']"}],"multipage":false,"notes":"The page is a JS shell (window.careers config, rspack bundle; recaptcha and turnstile toggles in config). The bundle's form definition names the standard fields firstname, lastname, email, phone, headline, summary, resume, cover_letter, address, photo; the EEOC survey after submission is [data-ui='eeoc-form'] with submit-eeoc / skip-eeoc buttons, so its appearance also marks the application as received. The exact submit button data-ui was not visible in the raw bundle; type=submit is the fallback."},{"id":"smartrecruiters","name":"SmartRecruiters","url_patterns":["https://jobs.smartrecruiters.com/*","https://careers.smartrecruiters.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of the job page (server-rendered; apply link a.js-oneclick to /oneclick-ui/company/<co>/publication/<uuid>); the oneclick-ui apply page returned 403 to curl","urls":["https://jobs.smartrecruiters.com/abbvie/3743990016045386"]},"form":"form","fields":{"first_name":["input[name='firstName']","#first-name","input[autocomplete='given-name']"],"last_name":["input[name='lastName']","#last-name","input[autocomplete='family-name']"],"email":["input[name='email']","#email","input[type='email']"],"phone":["input[name='phoneNumber']","#phone-number","input[type='tel']"],"resume":["input[type='file']"]},"continue_buttons":["button[type='button'].next","button[aria-label='Next' i]"],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"application was submitted"},{"text":"Your application has been received"}],"multipage":true,"notes":"The job page is server-rendered; Apply (a.js-oneclick, #st-apply) opens the oneclick-ui application, a JS app the curl fetch could not read (403). Field selectors beyond the autocomplete/type attributes are therefore fallbacks that defer to the label matcher. The apply flow is multi-step (contact, resume, questions, review)."},{"id":"recruitee","name":"Recruitee","url_patterns":["https://*.recruitee.com/o/*","https://jobs.*/o/*/c/new","https://*/o/*/c/new"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML (server-rendered form#offer-application-form)","urls":["https://jobs.channable.com/o/talent-acquisition-partner/c/new"]},"form":"form#offer-application-form","fields":{"name":["input[name='candidate.name']","input[autocomplete='name']"],"email":["input[name='candidate.email']","input[autocomplete='email']"],"phone":["input[name='candidate.phone']","input[autocomplete='tel']"],"resume":["input[name='candidate.cv']","input[type='file'][data-cy='fileInputField']"]},"continue_buttons":["button[data-cy='apply-button']"],"submit_buttons":["button[data-testid='submit-application-form-button']","form#offer-application-form button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been sent"},{"text":"We have received your application"}],"multipage":false,"notes":"Hosted on the company's own careers domain (jobs.<company>.com/o/<slug>/c/new) as well as <company>.recruitee.com. Server-rendered: candidate.name, candidate.email, candidate.phone, candidate.cv (file), candidate.coverLetterFile, custom questions as candidate.openQuestionAnswers.<id>.content (text/radio), consent checkbox candidate.agreements.0.consent. Submit is 'Send' (data-testid=submit-application-form-button). The job page's Apply button (data-cy=apply-button) switches to the application tab, it does not submit."},{"id":"workday","name":"Workday","url_patterns":["https://*.myworkdayjobs.com/*","https://*.myworkdaysite.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell only: <div id='root'>); selectors are Workday's data-automation-id attributes, the stable hooks Workday ships on every candidate-home form","urls":["https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"]},"form":"form","fields":{"first_name":["[data-automation-id='legalNameSection_firstName']"],"last_name":["[data-automation-id='legalNameSection_lastName']"],"email":["[data-automation-id='email']"],"phone":["[data-automation-id='phone-number']"],"street":["[data-automation-id='addressSection_addressLine1']"],"city":["[data-automation-id='addressSection_city']"],"state":["[data-automation-id='addressSection_countryRegion']"],"zip":["[data-automation-id='addressSection_postalCode']"],"country":["[data-automation-id='countryDropdown']","[data-automation-id='addressSection_country']"],"linkedin":["[data-automation-id='linkedinQuestion'] input","input[data-automation-id*='linkedin' i]"],"resume":["[data-automation-id='file-upload-input-ref']","input[type='file']"]},"continue_buttons":["[data-automation-id='bottom-navigation-next-button']","button[data-automation-id='pageFooterNextButton']"],"submit_buttons":["[data-automation-id='bottom-navigation-next-button'][aria-label*='Submit' i]","button[data-automation-id='pageFooterNextButton'][aria-label*='Submit' i]"],"success_markers":[{"text":"Congratulations"},{"text":"You have successfully submitted"},{"text":"application has been submitted"},{"text":"Thank you for applying"}],"multipage":true,"notes":"Workday renders everything client-side behind a candidate account; the application is a multi-page wizard (My Information, My Experience, Application Questions, Voluntary Disclosures, Self Identify, Review). The last page's Next button reads 'Submit', so submit_buttons match the same navigation button only when its label says Submit. Phone device type and country code are data-automation-id='phone-device-type' / 'country-phone-code' dropdowns the person picks. Sign-in and account creation are the person's (an auth wall aborts the fill)."},{"id":"icims","name":"iCIMS","url_patterns":["https://*.icims.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of the job page and the apply/login step inside the iCIMS content iframe (in_iframe=1)","urls":["https://careers-lw.icims.com/jobs/6713/financial-programmer/job?in_iframe=1","https://fdcnmcareers-ulta.icims.com/jobs/login?in_iframe=1"]},"frame":"iframe#icims_content_iframe","form":"form","fields":{"email":["form#enterEmailForm #email","#email","input[type='email']"],"phone":["#phoneNumber","input[name='phoneNumber']"],"first_name":["#firstname","input[name='firstname']","input[autocomplete='given-name']"],"last_name":["#lastname","input[name='lastname']","input[autocomplete='family-name']"],"resume":["input[type='file']"]},"continue_buttons":["#enterEmailSubmitButton","input.iCIMS_PrimaryButton[type='submit'][value='Next']","a.iCIMS_ApplyOnlineButton"],"submit_buttons":["input[type='submit'][value*='Submit' i]","button[type='submit'][class*='Submit' i]"],"success_markers":[{"text":"Thank you for applying"},{"text":"application has been received"},{"text":"successfully submitted"}],"multipage":true,"notes":"The career portal lives inside iframe#icims_content_iframe on the employer's page; the fill engine runs in the frame's own document. Apply starts with an email step (form#enterEmailForm: #email, #countryCode, #phoneNumber, submit #enterEmailSubmitButton 'Next') followed by a login or account-creation wall that is the person's. The later profile pages were not reachable without an account, so their fields fall through to the label matcher."},{"id":"bamboohr","name":"BambooHR","url_patterns":["https://*.bamboohr.com/careers/*","https://*.bamboohr.com/jobs/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell: <div id='poRoot'>, staticfe.bamboohr.com bundles; the application form chunk is lazy-loaded and not in the raw bundle)","urls":["https://fleetio.bamboohr.com/careers","https://staticfe.bamboohr.com/fe-main/prod/js/po.5d4a075d.dist.js"]},"form":"form","fields":{"first_name":["input[name='firstName']","#firstName","input[autocomplete='given-name']"],"last_name":["input[name='lastName']","#lastName","input[autocomplete='family-name']"],"email":["input[name='email']","#email","input[type='email']"],"phone":["input[name='phone']","#phone","input[type='tel']"],"resume":["input[type='file']"]},"continue_buttons":[],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"We have received your application"}],"multipage":false,"notes":"Careers pages are a React app (#poRoot) on <company>.bamboohr.com/careers/<id>; the application form itself is a lazy chunk the curl fetch could not read, so the field selectors are attribute fallbacks that defer to the label matcher."},{"id":"jobvite","name":"Jobvite","url_patterns":["https://jobs.jobvite.com/*","https://*.jobvite.com/*"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML of Jobvite's own careers site (jv-page markup, no open posting) and of customer slugs, which redirect to jobvite.com/support/job-seeker-support/?invalid=1 when the slug is not a live site; no apply form was reachable, so the fields are attribute fallbacks","urls":["https://jobs.jobvite.com/careers/jobvite","https://jobs.jobvite.com/jobvite/jobs"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='first' i]"],"last_name":["input[autocomplete='family-name']","input[name*='last' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["button.jv-button-primary[type='button']"],"submit_buttons":["button[type='submit']","input[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"received your application"}],"multipage":true,"notes":"Career sites are jobs.jobvite.com/<company>/job/<id> with the application at .../apply (jv-* markup). Only the site shell could be read live (no open posting), so the fields are attribute fallbacks that defer to the label matcher."},{"id":"taleo","name":"Oracle Taleo","url_patterns":["https://*.taleo.net/careersection/*","https://*.tbe.taleo.net/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"not readable: hilton.taleo.net/careersection/ answered 'Career Section Unavailable' and chp.tbe.taleo.net answered 'Come Back Soon' (HTTP 500); other *.taleo.net hosts did not resolve or redirected to the employer's new career site. The host families are real, no form was reachable, so no selector here is verified","urls":["https://hilton.taleo.net/careersection/hilton_external/jobsearch.ftl","https://chp.tbe.taleo.net/chp03/ats/careers/v2/searchResults?org=TEALIUM&cws=37"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='firstName' i]","input[id*='firstName' i]"],"last_name":["input[autocomplete='family-name']","input[name*='lastName' i]","input[id*='lastName' i]"],"email":["input[type='email']","input[name*='email' i]","input[id*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["input[type='submit'][value*='Continue' i]","button[type='submit'][title*='Continue' i]","a[id*='next' i]"],"submit_buttons":["input[type='submit'][value*='Submit' i]","button[type='submit'][title*='Submit' i]"],"success_markers":[{"text":"Thank you"},{"text":"submission has been received"},{"text":"application has been submitted"}],"multipage":true,"notes":"No live Taleo career section could be fetched for this version, so there are no verified selectors: the entry carries URL patterns, generic attribute fallbacks and the multi-step wizard hint, and every field defers to the label matcher. Taleo career sections sit behind a candidate sign-in (the person's)."},{"id":"successfactors","name":"SAP SuccessFactors","url_patterns":["https://*.successfactors.com/*","https://*.successfactors.eu/*","https://*/talentcommunity/apply/*","https://*/job/*/apply/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of a SuccessFactors career site (rmk markup): job page with a.dialogApplyBtn[href*='/talentcommunity/apply/']; the apply URL redirected to the site home (sign-in required)","urls":["https://jobs.adidas-group.com/job/Barcelona-Sales-Associate-Catalunya/1395009233/","https://jobs.adidas-group.com/talentcommunity/apply/1395009233/?locale=en_US"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='firstName' i]","input[id*='firstName' i]"],"last_name":["input[autocomplete='family-name']","input[name*='lastName' i]","input[id*='lastName' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["a.dialogApplyBtn","button[type='submit'][title*='Next' i]","input[type='submit'][value*='Next' i]"],"submit_buttons":["button[title*='Apply' i][type='submit']","input[type='submit'][value*='Apply' i]","button[type='submit'][title*='Submit' i]"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"successfully applied"}],"multipage":true,"notes":"Career sites run on the employer's own domain (jobs.<company>.com) with the SuccessFactors Recruiting Marketing markup; the application (talentcommunity/apply) sits behind the candidate profile sign-in, which the person completes. Beyond the apply link, no form fields were readable, so the fields defer to the label matcher."},{"id":"oracle_recruiting","name":"Oracle Cloud Recruiting","url_patterns":["https://*.oraclecloud.com/hcmUI/CandidateExperience/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (Knockout JS shell) + the live oj-hcm-ce bundle's apply-flow templates (section-personal-information, section-thank-you, apply-flow-pagination buttons)","urls":["https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/337010","https://static.oracle.com/cdn/fa/oj-hcm-ce/2607.26.262300322/js/main-minimal.js"]},"form":"form","fields":{"email":["input[type='email']","input[autocomplete='email']"],"phone":["input[type='tel']"],"first_name":["input[autocomplete='given-name']"],"last_name":["input[autocomplete='family-name']"],"resume":["input[type='file']"]},"continue_buttons":[".apply-flow-pagination__button:not(.apply-flow-pagination__submit-button)","button[class*='apply-flow-pagination__button']"],"submit_buttons":[".apply-flow-pagination__submit-button","button.submit-button[class*='apply-flow']"],"success_markers":[{"selector":".apply-flow-post-summary-thank-you"},{"selector":"[class*='section-thank-you']"},{"text":"Thank you for applying"},{"text":"application has been submitted"}],"multipage":true,"notes":"The apply flow starts with an email (then a verification code the person types), then the personal-information, experience, more-about-you and confirm sections, each a page of the wizard (apply-flow-pagination buttons). Field inputs are rendered from the employer's configuration with no stable ids, so they defer to the label matcher; the pagination and thank-you class names come from the live bundle's templates."},{"id":"eightfold","name":"Eightfold","url_patterns":["https://*.eightfold.ai/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (pcsx shell) + the live pcsxPwa bundle (#apply-form-container, data-test-id stepper hooks)","urls":["https://micron.eightfold.ai/careers","https://micron.eightfold.ai/gen/js/pcsxPwa.fa5d073d8912644a.js"]},"form":"#apply-form-container form","fields":{"first_name":["#apply-form-container input[autocomplete='given-name']","#apply-form-container input[name*='first' i]"],"last_name":["#apply-form-container input[autocomplete='family-name']","#apply-form-container input[name*='last' i]"],"email":["#apply-form-container input[type='email']","#apply-form-container input[name*='email' i]"],"phone":["#apply-form-container input[type='tel']","#apply-form-container input[name*='phone' i]"],"resume":["#apply-form-container input[type='file']","input[type='file']"]},"continue_buttons":["#apply-form-container button[type='button'][class*='next' i]","[data-test-id^='step-'] button[type='button']"],"submit_buttons":["#apply-form-container button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"application has been submitted"},{"text":"successfully submitted"}],"multipage":true,"notes":"Career sites are <company>.eightfold.ai/careers; the Smart Apply drawer renders inside #apply-form-container as a stepper (data-test-id='step-<n>'). Its inputs carry no stable ids in the shipped bundle, so the fields are attribute fallbacks scoped to the drawer that defer to the label matcher."},{"id":"phenom","name":"Phenom (careers.<company>.com)","url_patterns":["https://careers.*/*","https://jobs.*/*","https://*.phenompeople.com/*"],"detect_markers":["script[src*='phenompeople']","script[src*='phenom']","[data-ph-at-id]","#phApp"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of two Phenom career sites (phenompeople / phenomtrack markers); Phenom hands the application to the employer's ATS","urls":["https://www.pepsicojobs.com/main/jobs","https://jobs.cvshealth.com/us/en/home"]},"handoff":true,"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='first' i]"],"last_name":["input[autocomplete='family-name']","input[name*='last' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["a[data-ph-at-id='apply-link']","a.btn[href*='apply' i]","button[data-ph-at-id*='apply' i]"],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"application has been submitted"}],"multipage":true,"notes":"Phenom is the career-site layer on the employer's own domain (careers.<company>.com, jobs.<company>.com); it is recognised by its DOM markers rather than the host. 'Apply' hands off to the employer's ATS (Workday, SuccessFactors, iCIMS...), where that system's recipe takes over, so this entry mainly shows the apply hand-off hint and defers any on-site form to the label matcher."},{"id":"avature","name":"Avature","url_patterns":["https://*.avature.net/*","https://*/careers/Register*","https://*/careers/Login*","https://*/careers/JobDetail/*","https://*/careers/ApplicationMethods*"],"detect_markers":["meta[name='avature.portal.id']","input.WizardFieldInput","form.tpt_wizard"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML (server-rendered wizard form.tpt_wizard on a custom-domain Avature portal)","urls":["https://jobs.lenovo.com/en_US/careers/JobDetail/82554","https://jobs.lenovo.com/en_US/careers/Register?jobId=82554"]},"form":"form.tpt_wizard","fields":{"first_name":["form.tpt_wizard input[autocomplete='given-name']"],"last_name":["form.tpt_wizard input[autocomplete='family-name']"],"phone":["form.tpt_wizard input[type='tel'][autocomplete='tel']"],"email":["form.tpt_wizard input.EmailField","form.tpt_wizard input[autocomplete='email']"],"street":["form.tpt_wizard input.StreetField","form.tpt_wizard input[autocomplete='street-address']"],"city":["form.tpt_wizard input.CityField","form.tpt_wizard input[autocomplete='address-level2']"],"state":["form.tpt_wizard select[autocomplete='address-level1']"],"zip":["form.tpt_wizard input.PostalCodeField","form.tpt_wizard input[autocomplete='postal-code']"],"country":["form.tpt_wizard select.countryFieldSelect"],"resume":["form.tpt_wizard input[type='file']"]},"continue_buttons":["button.nextButton","button[name='next']"],"submit_buttons":["button[name='submit']","button.WizardButtonPrimary[type='submit'][name='finish']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"successfully submitted"}],"multipage":true,"notes":"Avature portals run on the employer's domain (jobs.<company>.com/en_US/careers/...) and are recognised by their meta avature.portal.* tags and the WizardField markup. Inputs have numeric ids (e.g. #353) that differ per portal, so the stable hooks are the autocomplete attributes and the field classes (TextField, EmailField, PostalCodeField, StreetField, CityField, countryFieldSelect). The wizard's Continue is button.nextButton[name=next]."},{"id":"adp","name":"ADP (Workforce Now / myjobs)","url_patterns":["https://workforcenow.adp.com/mascsr/default/mdf/recruitment/*","https://myjobs.adp.com/*","https://*.adp.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shells only: #recruitment_root on Workforce Now, <cx-root> on myjobs.adp.com)","urls":["https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html","https://myjobs.adp.com/"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='first' i]","input[id*='first' i]"],"last_name":["input[autocomplete='family-name']","input[name*='last' i]","input[id*='last' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["button[type='button'][class*='next' i]","button[aria-label='Next' i]"],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"successfully submitted"}],"multipage":true,"notes":"Both ADP candidate experiences are JS applications whose forms were not readable by curl, so the fields are attribute fallbacks that defer to the label matcher. Workforce Now career centres need a cid parameter per employer."}]}/*RECIPES_JSON_END*/;
  let RECIPES = RECIPES_FALLBACK;

  function setRecipes(json) {
    if (json && Array.isArray(json.systems)) RECIPES = json;
    return RECIPES;
  }

  // "https://*.icims.com/*" -> a regex. '*' spans anything, every other char is literal.
  function globToRe(pattern) {
    const esc = String(pattern).replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
    return new RegExp("^" + esc + "$", "i");
  }

  function qsaSafe(sel, root) {
    try { return Array.from((root || document).querySelectorAll(sel)); } catch (e) { return []; }
  }

  // ------------------------------------------------------ deep DOM: shadow roots + frames
  // Modern application forms do not keep every control in the top document. Some render
  // inside OPEN shadow roots (web components), and some (iCIMS, a few career sites) inside an
  // iframe. Every lookup below goes through searchRoots(): the document, every open shadow
  // root under it, and every SAME-ORIGIN frame's document, recursively. A CROSS-origin frame
  // cannot be reached from here at all (the browser forbids it); the desktop shell runs this
  // same engine inside such a frame through CDP instead (shell-electron/frames.js).
  // Closed shadow roots stay closed: they are invisible to script by design.
  let _rootsMemo = null;               // set for the length of one synchronous pass
  function shadowHosts(root) {
    return qsaSafe("*", root).filter((e) => e && e.shadowRoot);
  }
  function frameDocs(root) {
    const out = [];
    for (const f of qsaSafe("iframe, frame", root)) {
      let d = null;
      try { d = f.contentDocument; } catch (e) { d = null; }    // cross-origin: null or throws
      if (d && d.documentElement) out.push(d);
    }
    return out;
  }
  function searchRoots() {
    if (_rootsMemo) return _rootsMemo;
    const out = [];
    const seen = new Set();
    const visit = (root, depth) => {
      if (!root || seen.has(root) || depth > 8) return;
      seen.add(root);
      out.push(root);
      for (const h of shadowHosts(root)) visit(h.shadowRoot, depth + 1);
      for (const d of frameDocs(root)) visit(d, depth + 1);
    };
    visit(document, 0);
    return out;
  }
  function withRoots(fn) {
    if (_rootsMemo) return fn();
    _rootsMemo = null;
    _rootsMemo = searchRoots();
    try { return fn(); } finally { _rootsMemo = null; }
  }
  function qsaDeep(sel) {
    const out = [];
    for (const r of searchRoots()) for (const el of qsaSafe(sel, r)) out.push(el);
    return out;
  }
  // The document or shadow root an element lives in: its labels and aria ids live there too.
  function rootOf(el) {
    try { const r = el && el.getRootNode && el.getRootNode(); if (r && r.querySelector) return r; } catch (e) { /* old DOM */ }
    return document;
  }
  function byId(el, id) {
    if (!id) return null;
    const r = rootOf(el);
    try { if (r.getElementById) { const n = r.getElementById(id); if (n) return n; } } catch (e) { /* fragment */ }
    try { return document.getElementById(id); } catch (e) { return null; }
  }
  function labelFor(el) {
    if (!el || !el.id) return null;
    const sel = `label[for="${CSS.escape(el.id)}"]`;
    try { return rootOf(el).querySelector(sel) || null; } catch (e) { return null; }
  }
  function ariaLabelledText(el) {
    const al = el && el.getAttribute && el.getAttribute("aria-labelledby");
    if (!al) return "";
    return al.split(/\s+/).map((id) => { const n = byId(el, id); return n ? n.textContent : ""; })
      .filter(Boolean).join(" ").trim();
  }
  function rootText(r) {
    try { return String((r.body ? r.body.textContent : r.textContent) || ""); } catch (e) { return ""; }
  }
  // Visible iframes this engine CANNOT enter (cross-origin), not counting captcha widgets.
  function crossOriginFrames() {
    let n = 0;
    for (const r of searchRoots()) {
      for (const f of qsaSafe("iframe, frame", r)) {
        let d = null;
        try { d = f.contentDocument; } catch (e) { d = null; }
        if (d) continue;
        const src = norm(f.getAttribute && f.getAttribute("src"));
        if (/recaptcha|hcaptcha|captcha|turnstile|arkoselabs|challenges\.cloudflare/.test(src)) continue;
        const rect = f.getBoundingClientRect ? f.getBoundingClientRect() : {};
        if (rect.width > 40 && rect.height > 40) n++;
      }
    }
    return n;
  }
  // Fill refs must be unique across frames: the shell gives each frame's engine a prefix.
  let _refPrefix = "";
  function setRefPrefix(p) { _refPrefix = String(p || ""); return _refPrefix; }

  function hereUrl(url) {
    if (url) return String(url);
    try { return String((typeof location !== "undefined" && location.href) || ""); } catch (e) { return ""; }
  }

  // The recipe for this page: the first system whose URL pattern matches and, when the
  // entry also lists detect_markers (career sites on the employer's own domain, whose URL
  // says nothing), at least one marker is on the page.
  function recipeFor(url, doc) {
    const here = hereUrl(url);
    if (!here || !RECIPES || !Array.isArray(RECIPES.systems)) return null;
    for (const sys of RECIPES.systems) {
      if (!(sys.url_patterns || []).some((p) => globToRe(p).test(here))) continue;
      if (Array.isArray(sys.detect_markers) && sys.detect_markers.length) {
        if (!sys.detect_markers.some((m) => qsaSafe(m, doc).length)) continue;
      }
      return sys;
    }
    return null;
  }

  const CONTROL_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);
  function recipeTarget(sel, taken) {
    for (const el of qsaDeep(sel)) {
      if (!el || !CONTROL_TAGS.has(String(el.tagName || "").toUpperCase())) continue;
      if (taken.has(el)) continue;
      const t = String(el.tagName).toUpperCase() === "INPUT" ? String(el.type || "text").toLowerCase() : "";
      if (t === "file" || t === "hidden" || t === "checkbox" || t === "radio" || t === "password") continue;
      // A dropdown's search box (Greenhouse's #candidate-location is a react-select): typed text
      // is not a choice, so the widget layer picks the option instead of the recipe typing.
      if (isWidgetInput(el)) continue;
      if (!shown(el) || honeypot(el)) continue;
      return el;
    }
    return null;
  }

  // Values a recipe may ask for that the payload spells differently: a one-line
  // "City, ST" location, the street line as the address, the full name from its parts.
  function derivedFields(fields) {
    const f = Object.assign({}, fields || {});
    if (!f.location) {
      const bits = [f.city, f.state].filter(Boolean);
      f.location = bits.length ? bits.join(", ") : (f.address || "");
    }
    if (!f.street && f.address && !/,/.test(f.address)) f.street = f.address;
    if (!f.name && (f.first_name || f.last_name)) f.name = [f.first_name, f.last_name].filter(Boolean).join(" ");
    return f;
  }

  // Pass 1: the recipe's selectors, most specific first, one control per profile key.
  function planFromRecipe(recipe, fields, taken) {
    const plan = [];
    if (!recipe || !recipe.fields) return plan;
    const f = derivedFields(fields);
    for (const key of Object.keys(recipe.fields)) {
      if (key === "resume") continue;                    // a file input is never set from script
      const value = f[key];
      if (!value) continue;
      for (const sel of recipe.fields[key] || []) {
        const el = recipeTarget(sel, taken);
        if (!el) continue;
        taken.add(el);
        plan.push({ el, key, value, via: "recipe" });
        break;
      }
    }
    return plan;
  }

  function firstShown(selectors) {
    for (const sel of selectors || []) {
      for (const el of qsaDeep(sel)) if (el && shown(el)) return el;
    }
    return null;
  }

  // Outline the submit button so the person sees where THEIR click goes. Never clicks it.
  function highlightSubmit(recipe) {
    const btn = firstShown(recipe && recipe.submit_buttons);
    if (!btn) return false;
    injectStyle();
    try { btn.classList.add("tailor-submit-hint"); } catch (e) { /* non-element */ }
    return true;
  }

  // Is there a "Continue / Next" the person must press before more fields appear?
  function continueButton(recipe) {
    if (!recipe) return null;
    return firstShown(recipe.continue_buttons);
  }

  // Has the site said "application received"? Markers are text on the page, a selector, or
  // a URL pattern (Lever's /thanks). Checked after the PERSON submits; we never submit.
  function detectSuccess(recipe, url) {
    const r = recipe || recipeFor(url);
    const here = hereUrl(url);
    const body = norm(searchRoots().map(rootText).join(" ")).slice(0, 20000);
    const markers = (r && r.success_markers) || [];
    // A "thank you" PHRASE only counts once the form is gone: while the recipe's submit button
    // (or, with no recipe, a file upload) is still on the page, the person has not submitted,
    // whatever the page says elsewhere. A selector or URL marker is specific enough on its own.
    const formOpen = r ? !!firstShown(r.submit_buttons) : qsaDeep("input[type='file']").some(shown);
    for (const m of markers) {
      if (m.selector && qsaDeep(m.selector).length) return true;
      if (m.text && !formOpen && body.includes(norm(m.text))) return true;
      if (m.url_re) { try { if (new RegExp(m.url_re, "i").test(here)) return true; } catch (e) { /* bad re */ } }
    }
    if (!r && !formOpen) {       // no recipe: the generic wording most systems use
      return /thank you for applying|application (has been|was) (submitted|received)|we('| ha)?ve received your application/.test(body);
    }
    return false;
  }

  let _successTimer = null;
  function stopSuccessWatch() {
    if (_successTimer && typeof clearInterval === "function") clearInterval(_successTimer);
    _successTimer = null;
  }
  // Poll for the success marker after the fill (the person submits in their own time) and
  // report ONCE to the carrier, which tells the app to mark the job applied.
  function watchSuccess(cb, opts) {
    const recipe = (opts && opts.recipe) || recipeFor();
    const started = Date.now();
    const ttl = (opts && opts.ttl_ms) || 30 * 60 * 1000;
    stopSuccessWatch();
    const tick = () => {
      if (Date.now() - started > ttl) { stopSuccessWatch(); return; }
      let hit = false;
      try { hit = detectSuccess(recipe); } catch (e) { hit = false; }
      if (hit) {
        stopSuccessWatch();
        try { cb({ applied: true, url: hereUrl(), system: recipe ? recipe.id : "" }); } catch (e) { /* carrier */ }
      }
    };
    if (typeof setInterval === "function") _successTimer = setInterval(tick, 2000);
    return stopSuccessWatch;
  }

  // ---- learn from corrections: what the engine filled vs what the person then typed ----
  const _filledBy = new Map();        // el -> {key, value}
  const _corrections = new Map();     // key -> value the person changed it to
  let _correctionsCb = null;
  function noteFilled(el, key, value) {
    if (!el || !key) return;
    _filledBy.set(el, { key, value: String(value) });
  }
  function onPossibleCorrection(ev) {
    const el = ev && ev.target;
    const rec = el && _filledBy.get(el);
    if (!rec) return;
    const now = String(el.value || "").trim();
    if (!now || now === rec.value) { _corrections.delete(rec.key); return; }
    _corrections.set(rec.key, now);
    if (_correctionsCb) { try { _correctionsCb({ key: rec.key, value: now, was: rec.value }); } catch (e) { /* carrier */ } }
  }
  function watchCorrections(cb) {
    _correctionsCb = cb || null;
    if (document && typeof document.addEventListener === "function" && !watchCorrections._armed) {
      document.addEventListener("change", onPossibleCorrection, true);
      watchCorrections._armed = true;
    }
    // "change" does not cross a shadow-root or frame boundary: listen in each of those too.
    for (const r of searchRoots()) {
      if (r === document || _armedRoots.has(r) || typeof r.addEventListener !== "function") continue;
      r.addEventListener("change", onPossibleCorrection, true);
      _armedRoots.add(r);
    }
  }
  const _armedRoots = new WeakSet();
  // Corrections so far, as {key: value}; the carrier offers to save them to the profile.
  function takeCorrections() {
    const out = {};
    for (const [k, v] of _corrections) out[k] = v;
    _corrections.clear();
    return out;
  }

  let _styled = false;
  function injectStyle() {
    if (_styled || !document || typeof document.createElement !== "function" || !document.head) return;
    const st = document.createElement("style");
    st.textContent = ".tailor-submit-hint{outline:3px solid #f79009 !important;outline-offset:3px !important;"
                   + "box-shadow:0 0 0 6px rgba(247,144,9,.25) !important;}";
    document.head.appendChild(st);
    _styled = true;
  }

  // --- field categories -> where each maps in the /api/profile/autofill payload ----
  // Ordered most-specific first; the first category whose signal matches wins.
  const CATEGORIES = [
    { key: "email",      ac: ["email"],                 re: /e-?mail/ },
    { key: "phone",      ac: ["tel", "tel-national"],   re: /phone|mobile|\btel\b|telephone/ },
    { key: "first_name", ac: ["given-name"],            re: /first.?name|given.?name|\bfname\b|forename/ },
    { key: "last_name",  ac: ["family-name"],           re: /last.?name|family.?name|surname|\blname\b/ },
    { key: "linkedin",   ac: [],                        re: /linkedin/ },
    { key: "github",     ac: [],                        re: /github/ },
    { key: "website",    ac: ["url"],                   re: /portfolio|personal.?(web)?site|\bwebsite\b|\bblog\b|personal.?url/ },
    { key: "country",    ac: ["country", "country-name"], re: /\bcountry\b/ },
    { key: "city",       ac: ["address-level2"],        re: /\bcity\b|\btown\b/ },
    { key: "state",      ac: ["address-level1"],        re: /\bstate\b|province|\bregion\b/ },
    { key: "zip",        ac: ["postal-code"],           re: /\bzip\b|postal|post.?code/ },
    { key: "street",     ac: ["street-address", "address-line1"], re: /street|address.?line|\baddress\b|\baddr\b/ },
    // full name last: only wins if a field says "name" but isn't first/last/user/company.
    { key: "name",       ac: ["name"],                  re: /full.?name|your.?name|legal.?name|\bname\b/ },
  ];
  // Fields we must never treat as a "name" (avoids filling username / company / referral).
  const NAME_VETO = /user|company|employer|referr|friend|contact.?name|emergency|search|coupon|reason/;

  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();

  // Everything textual that hints what an input is for.
  function signature(el) {
    const bits = [el.name, el.id, el.placeholder, el.getAttribute("aria-label")];
    const al = el.getAttribute("aria-labelledby");
    if (al) al.split(/\s+/).forEach((id) => {
      const n = byId(el, id); if (n) bits.push(n.textContent);
    });
    if (el.id) {
      const lab = labelFor(el);
      if (lab) bits.push(lab.textContent);
    }
    let p = el.closest("label");                       // wrapping <label>
    if (p) bits.push(p.textContent);
    // Nearest preceding label-ish text in the same field group (Greenhouse/Ashby markup).
    const grp = el.closest("div,fieldset,li,section");
    if (grp) {
      const lab = grp.querySelector("label, .label, legend");
      if (lab) bits.push(lab.textContent);
    }
    return norm(bits.filter(Boolean).join(" · "));
  }

  function categorize(el, sig) {
    const ac = norm(el.getAttribute("autocomplete"));
    for (const c of CATEGORIES) {
      if (ac && c.ac.includes(ac)) {
        if (c.key === "name" && NAME_VETO.test(sig)) continue;
        return c.key;
      }
    }
    for (const c of CATEGORIES) {
      if (c.re.test(sig)) {
        if (c.key === "name" && NAME_VETO.test(sig)) continue;
        return c.key;
      }
    }
    return null;
  }

  const FILLABLE_TYPES = new Set(["text", "email", "tel", "url", "search", ""]);

  // Is this control one a PERSON could see and type into? Anything else is left alone,
  // and not only because filling it is pointless: an input with no size, parked off the
  // canvas, or drawn at opacity 0 is the classic anti-spam honeypot, and a value in it
  // gets the whole application binned as a bot submission. So the rule is strict on
  // purpose. (Visibility predicate adapted from AIHawk's snapshot code, MIT; see NOTICES.md.)
  function shown(el) {
    if (el.disabled || el.readOnly) return false;
    const rect = el.getBoundingClientRect() || {};
    if (!rect.width && !rect.height) return false;                        // no box at all
    if (parkedOffCanvas(el, rect)) return false;                         // parked off-canvas
    // An element inside a same-origin frame is styled by ITS window.
    let view = window;
    try { if (el.ownerDocument && el.ownerDocument.defaultView) view = el.ownerDocument.defaultView; } catch (e) { view = window; }
    const gcs = (view && typeof view.getComputedStyle === "function") ? view.getComputedStyle(el) : null;
    if (gcs && (gcs.display === "none" || gcs.visibility === "hidden" ||
                gcs.opacity === "0")) return false;
    return true;
  }

  // An anti-spam trap that says so in its label (Workday: "Enter website. This input is for
  // robots only, do not enter if you're human."). A value in it marks the application as a bot's.
  const HONEYPOT_RE = /robots? only|if you('|\u2019| a)re (a )?human|leave (this|it)( field)? (blank|empty)/i;
  function honeypot(el) {
    const bits = [el.getAttribute("aria-label"), el.placeholder, el.name];
    if (el.id) { const l = labelFor(el); if (l) bits.push(l.textContent); }
    const p = el.closest && el.closest("label"); if (p) bits.push(p.textContent);
    bits.push(ariaLabelledText(el));
    return HONEYPOT_RE.test(bits.filter(Boolean).join(" "));
  }

  // Off the PAGE (left:-9999px), not merely scrolled out of view. The rect is relative to the
  // viewport, so a field the person scrolled past has a negative bottom too: add the scroll back
  // (a real page filled after scrolling to the bottom skipped every field above the fold).
  function parkedOffCanvas(el, rect) {
    if (rect.right === undefined || rect.bottom === undefined) return false;
    let v = window;
    try { v = (el.ownerDocument && el.ownerDocument.defaultView) || window; } catch (e) { v = window; }
    const sx = Number(v.scrollX || v.pageXOffset || 0), sy = Number(v.scrollY || v.pageYOffset || 0);
    if (rect.right + sx <= 0) return true;
    if (rect.bottom + sy > 0) return false;
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (p.scrollTop > 0) return false;                  // scrolled past inside a scrolling panel
    }
    return true;
  }

  // The SITE's own search box (a careers page's "Search jobs" and "City, state" pair), not a
  // question on an application. On a job description page these are often the only inputs, and
  // a fill once typed the person's city into Amazon's job search. Never filled, never planned.
  const SITE_SEARCH_RE = /search (for )?jobs|job search|keyword|search by (title|keyword)|find (your next )?jobs?\b/i;
  function siteSearch(el) {
    if (!el || !el.closest) return false;
    if (el.closest('[role="search"], search, header, nav')) return true;
    const form = el.form || el.closest("form");
    if (form) {
      const bits = [form.getAttribute("action"), form.getAttribute("role"), form.getAttribute("name"),
                    form.getAttribute("aria-label"), form.id, String(form.className || "")];
      if (/search/i.test(bits.filter(Boolean).join(" "))) return true;
    }
    const own = [el.getAttribute("name"), el.id, el.getAttribute("placeholder"), el.getAttribute("aria-label")];
    return SITE_SEARCH_RE.test(own.filter(Boolean).join(" "));
  }

  // Is the page open an application form, or a job page whose only boxes are the site's own
  // search? A form asks for the person (an email or phone, a name, a resume upload) or asks
  // questions (radios, dropdowns, an essay box), or the page is a known application system. A page with none of those is not filled at all; the
  // person is told to press the site's Apply button first.
  const PERSON_FIELD_RE = /e-?mail|phone|mobile|first name|last name|full name|legal name|\bname\b|resume|\bcv\b/i;
  function applicationForm() {
    return withRoots(() => {
      if (recipeFor()) return true;
      if (hasResumeUpload()) return true;
      // The page footer (a "get job alerts" email box, the site's language picker) is not an
      // application. Amazon's footer is a div, so match the footer by role, id and class too.
      const fields = extractForm({}).filter((f) => {
        const el = elByRef(f.ref);
        if (el && el.closest && el.closest('footer, [role="contentinfo"], [id*="footer" i], [class*="footer" i]')) return false;
        return !/alert|newsletter|subscribe|^\s*(language|english)\s*$/i.test(f.label || "");
      });
      if (fields.some((f) => f.type === "email" || f.type === "tel" || PERSON_FIELD_RE.test(f.label || ""))) return true;
      // A page of screening questions (radios, dropdowns, an essay box) is a form too.
      if (fields.some((f) => ["radio", "select", "textarea", "file"].includes(f.type))) return true;
      return fields.filter((f) => f.type !== "checkbox").length >= 2;
    });
  }
  // The site's own button to start the application, for the "press Apply first" message.
  function applyButtonLabel() {
    const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const el = [...document.querySelectorAll('a, button, [role="button"], input[type="submit"]')]
      .find((e) => vis(e) && /^(apply|apply now|apply for (this )?job|apply online|start (your )?application|i'?m interested)$/i
        .test(String(e.innerText || e.value || "").replace(/\s+/g, " ").trim()));
    return el ? String(el.innerText || el.value).replace(/\s+/g, " ").trim().slice(0, 40) : "";
  }

  function candidates() {
    const out = [];
    qsaDeep("input, textarea, select").forEach((el) => {
      if (!shown(el) || honeypot(el) || siteSearch(el)) return;
      const tag = el.tagName.toLowerCase();
      if (tag === "input") {
        const t = (el.type || "text").toLowerCase();
        if (!FILLABLE_TYPES.has(t)) return;                // skip file/checkbox/radio/password/submit
        if (isWidgetInput(el)) return;                     // a custom dropdown's search box: fillWidgets
      }
      out.push(el);
    });
    return out;
  }

  // React/Vue-safe value set: use the native setter, then fire input+change so the
  // framework's state updates (a plain el.value = x is ignored by controlled inputs).
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
                : el.tagName === "SELECT"   ? HTMLSelectElement.prototype
                :                              HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillSelect(el, value) {
    const want = norm(value);
    for (const opt of el.options) {
      if (norm(opt.value) === want || norm(opt.textContent) === want ||
          norm(opt.textContent).startsWith(want)) {
        setNativeValue(el, opt.value);
        return true;
      }
    }
    return false;
  }

  function markFilled(el) {
    el.classList.add("tailor-filled");
    setTimeout(() => el.classList.remove("tailor-filled"), 2500);
  }

  // Plan the fill without applying it, used both to decide whether to offer the panel
  // and to run it. Returns [{el, key, value}].
  function planFill(fields, opts) {
    const recipe = (opts && "recipe" in opts) ? opts.recipe : recipeFor();
    const taken = new Set();
    const plan = planFromRecipe(recipe, fields, taken);     // 1. the recipe for this site
    // A key the recipe placed is DONE: the recipe knows the form, so the label pass must not
    // pour the same phone into "Home phone" too, or the same name into a reference's name.
    const placed = new Set(plan.map((p) => p.key));
    for (const el of candidates()) {                         // 2. the label patterns
      if (taken.has(el)) continue;
      const sig = signature(el);
      if (!sig) continue;
      const key = categorize(el, sig);
      if (!key || placed.has(key)) continue;
      const value = fields[key];
      if (!value) continue;
      if (!labelFits(el, key)) continue;
      taken.add(el);
      plan.push({ el, key, value, via: "label" });
    }
    return plan;
  }

  // A contact field's label is short ("Address", "Phone"). A long question that merely mentions
  // a keyword ("...what challenges did you face and how did you ADDRESS them?", a Workable essay)
  // is not one, and an essay box only ever takes a street address. The browser's autocomplete
  // hint, when the page gives one, is trusted over the label.
  function labelFits(el, key) {
    const ac = norm(el.getAttribute("autocomplete"));
    if (ac && CATEGORIES.some((c) => c.ac.includes(ac))) return true;
    const lab = cleanLabel(rawLabel(el));
    if (lab.length > 60) return false;
    if (tagOf(el) === "TEXTAREA" && key !== "street") return false;
    return true;
  }

  function applyFill(plan) {
    let filled = 0;
    for (const { el, key, value } of plan) {
      if (String(el.value || "").trim()) continue;      // never overwrite what you typed
      const ok = el.tagName === "SELECT" ? fillSelect(el, value)
                                         : (setNativeValue(el, value), true);
      if (ok) { markFilled(el); noteFilled(el, key, value); filled++; }
    }
    return filled;
  }

  // Which saved text answer (if any) a field's question label asks for. Pure + exported so
  // the matching is unit-testable without a DOM.
  function answerKeyForLabel(label) {
    const q = norm(label || "");
    if (!q) return null;
    for (const a of TEXT_ANSWERS) if (a.re.test(q)) return a.key;
    return null;
  }

  // Fill saved short text answers (years of experience, desired salary, earliest start)
  // into a matching EMPTY text/number field. Never a radio/select/textarea, never a value
  // you already typed, never a free-text essay we don't have an exact answer for.
  function fillTextAnswers(answers) {
    if (!answers) return 0;
    let filled = 0;
    for (const el of candidates()) {
      const tag = el.tagName.toLowerCase();
      if (tag !== "input") continue;
      const t = (el.type || "text").toLowerCase();
      if (!["text", "number", "tel"].includes(t)) continue;
      if (String(el.value || "").trim()) continue;          // never overwrite what you typed
      const key = answerKeyForLabel(rawLabel(el) || groupQuestion(el));
      if (!key) continue;
      const value = answers[key];
      if (!value) continue;
      setNativeValue(el, value);
      markFilled(el);
      filled++;
    }
    return filled;
  }

  // ---------------------------------------------------------- screening & EEO
  // Radio-group / dropdown questions with KNOWN answers you saved once (work
  // authorization, sponsorship need) or a neutral EEO "decline" choice. We only ever
  // pick Yes/No or a decline option, never a free-text guess, never a demographic value
  // you didn't choose.
  const SCREEN = [
    { key: "needs_sponsorship", re: /sponsor|require.*visa|visa.*(status|sponsor|support)|employment visa|immigration (support|sponsor)/ },
    { key: "work_authorized",   re: /authoriz(ed|ation) to work|legally.*(authoriz|work)|eligible to work|right to work|work authoriz|lawfully (able|authorized)/ },
    { key: "willing_to_relocate", re: /relocat|willing to move|open to relocat/ },
    { key: "over_18", re: /\b18\s*(years)?\s*(or older|and over|of age|\+)|at least 18|are you 18|18 years of age|over 18/ },
  ];
  // "Will you NOW OR IN THE FUTURE require sponsorship?" vs "Do you require sponsorship?"
  const FUTURE_RE = /now or in the future|in the future|will you (ever|at any time)|future/;

  // The question patterns are DATA too (recipes.json "question_patterns": work_auth_us,
  // needs_sponsorship, eeo_decline). When the table carries them they drive the routing below;
  // the built-ins above and EEO_TOPICS/DECLINE_RE are the fallback for an older table.
  const QP_KEY = { work_authorized: "work_auth_us", needs_sponsorship: "needs_sponsorship" };
  function dataRe(src, field, fallback) {
    if (!src || !src[field]) return fallback;
    try { return new RegExp(src[field], "i"); } catch (e) { return fallback; }
  }
  function screenRules() {
    const qp = RECIPES && RECIPES.question_patterns;
    if (!qp) return SCREEN;
    return SCREEN.map((s) => (QP_KEY[s.key] ? { key: s.key, re: dataRe(qp[QP_KEY[s.key]], "label_re", s.re) } : s));
  }
  function futureRe() {
    const qp = RECIPES && RECIPES.question_patterns;
    return dataRe(qp && qp.needs_sponsorship, "future_re", FUTURE_RE);
  }
  function eeoRes() {
    const qp = RECIPES && RECIPES.question_patterns;
    return { topics: dataRe(qp && qp.eeo_decline, "label_re", EEO_TOPICS),
             decline: dataRe(qp && qp.eeo_decline, "option_re", DECLINE_RE) };
  }

  // The saved answer for a screening question, as Yes/No, or null to leave it to the person.
  // work_auth_us is the profile's name for work_authorized (both are honoured). Sponsorship has
  // three honest answers: Yes (now), No, Future (not now, e.g. on OPT, but later). "Future"
  // answers YES to a now-or-future question and is left blank on a present-tense one, since
  // either Yes or No would be wrong.
  function screeningAnswer(screening, key, question) {
    if (!screening) return null;
    let v = screening[key];
    if (key === "work_authorized" && !v) v = screening.work_auth_us;
    v = String(v || "");
    if (v === "Yes" || v === "No") return v;
    if (key === "needs_sponsorship" && v === "Future") return futureRe().test(question) ? "Yes" : null;
    return null;
  }

  // Short free-text answers saved once (years of experience, desired salary, earliest
  // start) matched to a text/number field by its question text. Same conservative rule as
  // everything else: only ever fill an EMPTY field, never overwrite what the person typed.
  const TEXT_ANSWERS = [
    { key: "years_experience", re: /years?\s*(of)?\s*(work|professional|relevant)?\s*experience|experience.*years|how many years/ },
    { key: "desired_salary",   re: /salary|compensation|desired pay|expected pay|pay expectation/ },
    { key: "earliest_start",   re: /start date|available to start|when can you (start|begin)|earliest start|notice period|availability/ },
    { key: "hear_about_us",    re: /how did you hear|where did you hear|hear about (us|this|the (job|role|position|opening))|referral source|how.*find (us|this|out about)/ },
  ];
  const EEO_TOPICS = /gender|\brace\b|ethnic|veteran|disab/;
  const DECLINE_RE = /decline|prefer not|not to (answer|identify|disclose)|don'?t (wish|want)|do not (wish|want)|not declared/;
  const YES_RE = /^\W*(yes|y|i am\b|i do\b|true)/;
  const NO_RE  = /^\W*(no|n|i am not|i do not|i'?m not|false)/;

  function radioOptionText(r) {
    if (r.id) { const l = labelFor(r); if (l) return norm(l.textContent); }
    const p = r.closest("label"); if (p) return norm(p.textContent);
    if (r.nextElementSibling) return norm(r.nextElementSibling.textContent);
    return norm(r.value);
  }

  // The QUESTION text for a control (legend / group label), distinct from an option's
  // own label. Used to route a group to a screening/EEO category. Walks up from the control
  // to the question's own box: the smallest ancestor that holds a label-ish element which is
  // not the option's own wrapping label, and no OTHER question's controls (so a radio in
  // Lever's div.application-question > ul > li > label markup still finds its question, and
  // the walk never reaches up to another question's text).
  const QUESTION_LABEL = "legend, label:not([for]), .label, [class*='question'], [class*='label'], [class*='title'], [class*='prompt']";
  // A <label for=...> that names a radio or checkbox OPTION (Ashby: "I currently reside within
  // commuting distance"), never the question above the options.
  function isOptionLabel(lab) {
    if (tagOf(lab) !== "LABEL" || !lab.htmlFor) return false;
    const t = byId(lab, lab.htmlFor);
    return !!(t && /^(radio|checkbox)$/i.test(String(t.type || "")));
  }
  function groupQuestion(control) { return norm(groupQuestionText(control)); }
  // The question in its own case (what the plan and the "left for you" list show).
  function groupQuestionText(control) {
    const fs = control.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg && cleanText(lg.textContent)) return cleanText(lg.textContent); }
    const ctype = String(control.type || "").toLowerCase();
    const radioName = (ctype === "radio" || ctype === "checkbox") && control.name ? control.name : null;
    let node = control.parentElement;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      const tag = String(node.tagName || "").toUpperCase();
      if (tag === "FORM" || tag === "BODY" || tag === "HTML") break;
      const others = qsaSafe("input, select, textarea, [role='combobox'], [role='radiogroup'], [role='checkbox']", node).filter((o) =>
        o !== control && !control.contains(o) && !o.contains(control) && String(o.type || "").toLowerCase() !== "hidden" &&
        !(radioName && String(o.type || "").toLowerCase() === ctype && o.name === radioName));
      if (others.length) break;                       // past this question's box
      for (const lab of qsaSafe(QUESTION_LABEL, node)) {
        // An option's label wraps its control (this one's or a sibling's), or points at one with
        // for=; the question's does neither.
        if (lab.contains(control) || control.contains(lab) || qsaSafe("input, select, textarea", lab).length) continue;
        if (isOptionLabel(lab)) continue;
        const t = cleanText(lab.textContent);
        if (t) return t;
      }
      const aria = node.getAttribute && node.getAttribute("aria-label"); if (aria) return cleanText(aria);
    }
    return cleanText(control.getAttribute("aria-label") || "");
  }
  const cleanText = (t) => String(t || "").replace(/\s+/g, " ").trim();

  function radioGroups() {
    const groups = new Map();
    let n = 0;
    qsaDeep('input[type="radio"]').forEach((r) => {
      if (r.disabled) return;
      const key = r.name || ("__anon" + (n++));
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    return [...groups.values()];
  }

  // Set a radio via the native `checked` setter (bypassing React/Vue's value tracker,
  // which ignores a plain `r.checked = true`), then fire the events the framework binds.
  function setChecked(r) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked").set;
    setter.call(r, true);
    r.dispatchEvent(new Event("click", { bubbles: true }));
    r.dispatchEvent(new Event("input", { bubbles: true }));
    r.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function pickRadio(radios, matchFn) {
    if (radios.some((r) => r.checked)) return false;      // already answered, leave it
    for (const r of radios) {
      if (matchFn(radioOptionText(r))) {
        setChecked(r);
        markFilled(r.closest("label") || r.parentElement || r);
        return true;
      }
    }
    return false;
  }

  function pickSelectOption(sel, matchFn) {
    const cur = norm(sel.value);
    if (cur && !/^(select|choose|--|please)/.test(cur)) return false;   // already chosen
    for (const opt of sel.options) {
      if (!opt.value && !norm(opt.textContent)) continue;
      if (matchFn(norm(opt.textContent)) || matchFn(norm(opt.value))) {
        setNativeValue(sel, opt.value);
        markFilled(sel);
        return true;
      }
    }
    return false;
  }

  // Fill saved Yes/No screening answers into whichever radio group or select asks them.
  function fillScreening(screening) {
    if (!screening) return 0;
    let count = 0;
    const rules = screenRules();
    const routeAndFill = (control, radiosOrNull) => {
      const q = groupQuestion(control) || signature(control);
      // A combined question ("authorized to work WITHOUT sponsorship?") matches both the
      // work-authorization AND sponsorship patterns, with OPPOSITE polarity, we can't
      // answer it safely, so leave it for the person rather than fill an inverted answer.
      if (rules.filter((s) => s.re.test(q)).length > 1) return;
      for (const s of rules) {
        if (!s.re.test(q)) continue;
        const answer = screeningAnswer(screening, s.key, q);
        if (!answer) return;
        const want = answer === "Yes" ? YES_RE : NO_RE;
        const ok = radiosOrNull ? pickRadio(radiosOrNull, (t) => want.test(t))
                                : pickSelectOption(control, (t) => want.test(t));
        if (ok) count++;
        return;
      }
    };
    for (const radios of radioGroups()) routeAndFill(radios[0], radios);
    qsaDeep("select").forEach((sel) => routeAndFill(sel, null));
    return count;
  }

  // Set EEO / demographic self-ID questions to the neutral "decline" option. Never
  // fills a gender/race/veteran/disability VALUE, only the decline choice you opted into.
  function fillEeo() {
    let count = 0;
    const { topics, decline } = eeoRes();
    for (const radios of radioGroups()) {
      if (topics.test(groupQuestion(radios[0])) && pickRadio(radios, (t) => decline.test(t))) count++;
    }
    qsaDeep("select").forEach((sel) => {
      const q = groupQuestion(sel) || signature(sel);
      if (topics.test(q) && pickSelectOption(sel, (t) => decline.test(t))) count++;
    });
    return count;
  }

  // A résumé/CV file input we can SEE but cannot fill (browsers block setting file inputs
  // from script). Surface it so the person attaches their tailored PDF themselves.
  function hasResumeUpload() {
    for (const f of qsaDeep('input[type="file"]')) {
      if (f.disabled) continue;
      const sig = signature(f) + " " + norm(f.getAttribute("accept") || "");
      if (/resume|résumé|\bcv\b|cover.?letter|upload|attach|\.pdf|\.docx?/.test(sig)) return true;
      if (f.getBoundingClientRect().width > 0) return true;   // a visible file input on an apply form
    }
    return false;
  }

  // ---------------------------------------------------------- essay questions
  // The QUESTION prompt for a control, in its original case (for sending to the model
  // and displaying), unlike signature()/groupQuestion(), which lowercase for matching.
  //
  // Order: a radio/checkbox takes its group's <legend>; any other control its OWN label first
  // (label[for], aria-labelledby, a wrapping label), and only then the fieldset's legend: on
  // Greenhouse's new boards the phone number's country picker sits in <fieldset><legend>Phone
  // but is labelled "Country" itself.
  function rawLabel(el) {
    const type = String(el.type || "").toLowerCase();
    const legend = () => {
      const fs = el.closest("fieldset");
      const lg = fs && fs.querySelector("legend");
      return lg ? cleanText(lg.textContent) : "";
    };
    let out = (type === "radio" || type === "checkbox") ? legend() : "";
    if (!out && el.id) { const l = labelFor(el); if (l) out = labelText(l, el); }
    if (!out) out = cleanText(ariaLabelledText(el));
    if (!out) { const p = el.closest("label"); if (p) out = labelText(p, el); }
    if (!out) out = legend();
    if (!out) {
      const grp = el.closest("div,li,section,fieldset");
      const l = grp && grp.querySelector("label, legend, .label, [class*='question']");
      if (l) out = labelText(l, el);
    }
    if (!out) out = cleanText(el.getAttribute("aria-label") || el.placeholder || "");
    // A file input's own label is often just its button ("Attach", "Upload"): the question is
    // the group's ("Resume/CV").
    if (type === "file" && (!out || GENERIC_FILE_RE.test(out))) out = fileGroupLabel(el) || out;
    return out;
  }
  const GENERIC_FILE_RE = /^(attach|upload|browse|choose|select|add|drop|drag)\b[\w\s]{0,12}$/i;
  // The text a person SEES in a label: its label-ish heading when it wraps the control (Lever:
  // <label><div class="application-label">Full name</div><div><input></div></label>), else its
  // visible text without a wrapped <select>'s options or hidden status messages.
  const LABELISH = "[class*='label'], [class*='question'], [class*='title'], [class*='prompt'], legend";
  function labelText(lab, el) {
    if (el && lab.contains(el)) {
      for (const h of qsaSafe(LABELISH, lab)) {
        if (h.contains(el) || qsaSafe("input, select, textarea", h).length) continue;
        const t = visibleText(h);
        if (t) return t;
      }
    }
    return visibleText(lab);
  }
  function visibleText(node) {
    const out = [];
    const doc = node.ownerDocument || document;
    let w = null;
    try { w = doc.createTreeWalker(node, 4 /* NodeFilter.SHOW_TEXT */); } catch (e) { return cleanText(node.textContent); }
    const okEl = new Map();
    const visible = (p) => {
      if (okEl.has(p)) return okEl.get(p);
      let ok = true;
      for (let e = p; e; e = e.parentElement) {
        if (/^(SELECT|OPTION|TEXTAREA|SCRIPT|STYLE|NOSCRIPT|TEMPLATE|SVG)$/.test(tagOf(e))) { ok = false; break; }
        if (e !== node && (e.hidden || (e.getAttribute && e.getAttribute("aria-hidden") === "true"))) { ok = false; break; }
        try {
          const cs = viewOf(e).getComputedStyle(e);
          if (cs && (cs.display === "none" || cs.visibility === "hidden")) { ok = false; break; }
        } catch (err) { /* no styles */ }
        if (e === node) break;
      }
      okEl.set(p, ok);
      return ok;
    };
    for (let t = w.nextNode(); t; t = w.nextNode()) {
      if (t.parentElement && visible(t.parentElement)) out.push(t.nodeValue);
    }
    return cleanText(out.join(" "));
  }
  // The question of a file input: the labelled group around it, or the nearest heading-like
  // text in its box that is not another button word.
  function fileGroupLabel(el) {
    const boxes = [];
    for (let node = el.parentElement, i = 0; node && i < 6; i++, node = node.parentElement) {
      if (/^(FORM|BODY)$/.test(tagOf(node))) break;
      if (qsaSafe("input[type='file']", node).length > 1) break;      // reached another upload's box
      boxes.push(node);
    }
    for (const node of boxes) {                         // Greenhouse: <div role=group aria-labelledby>
      const al = cleanText(ariaLabelledText(node));
      if (al && !GENERIC_FILE_RE.test(al)) return al;
    }
    for (const node of boxes) {
      for (const h of qsaSafe("label, legend, " + LABELISH, node)) {
        if (h.contains(el)) continue;
        if (tagOf(h) === "LABEL" && h.htmlFor && h.htmlFor !== el.id) continue;   // another control's label
        const t = visibleText(h);
        if (t && !GENERIC_FILE_RE.test(t) && t.length < 120) return t;
      }
    }
    return "";
  }

  function answerableTextareas() {
    const out = [];
    qsaDeep("textarea").forEach((t) => {
      if (t.disabled || t.readOnly) return;
      if (t.getBoundingClientRect().width === 0) return;
      const q = rawLabel(t).replace(/\s+/g, " ").replace(/\*+$/, "").trim();
      if (!q || q.length < 5) return;                 // needs a real question to answer
      out.push({ el: t, question: q, cover: /cover.?letter/i.test(q) });
    });
    return out;
  }

  // Lightweight job context from the page (title + any description block) so drafted
  // answers reflect THIS role. The profile does the grounding; this just adds relevance.
  function pageJobContext() {
    const titleEl = document.querySelector(
      'h1, [class*="posting-headline"], [class*="job-title"], [data-testid*="title"]');
    const title = (titleEl ? titleEl.textContent : document.title || "").replace(/\s+/g, " ").trim();
    let desc = "";
    for (const s of ['[class*="job-description"]', '[class*="posting-description"]',
                     '[class*="opening"]', '[class*="description"]', "main", "#content"]) {
      const el = document.querySelector(s);
      if (el && el.textContent.trim().length > 200) { desc = el.textContent.replace(/\s+/g, " ").trim(); break; }
    }
    return (title + "\n\n" + desc).trim().slice(0, 4000);
  }

  // ---------------------------------------------------------- anti-bot wall
  // A captcha or auth/login wall means the site wants a HUMAN here. We NEVER try to get past it
  // (no evasion, no stealth, per CLAUDE.md §7): we detect it and drop the whole fill to the person.
  // Only a captcha the person can SEE is a wall. Invisible reCAPTCHA v3 / Turnstile score checks
  // load a zero-size, hidden frame on many career sites (Lam Research) and never ask anything:
  // stopping on them sent people looking for a captcha that was not there.
  function shownFrame(f) {
    try {
      const r = f.getBoundingClientRect();
      if (r.width < 20 || r.height < 20) return false;
      const cs = getComputedStyle(f);
      if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) return false;
      return !(f.closest && f.closest('[aria-hidden="true"]'));
    } catch (e) { return true; }
  }
  function pageBlocker() {
    for (const f of qsaDeep("iframe")) {
      const src = norm(f.getAttribute && f.getAttribute("src"));
      if (/recaptcha|hcaptcha|\bcaptcha\b|turnstile|arkoselabs|funcaptcha|challenges\.cloudflare/.test(src) && shownFrame(f))
        return "captcha";
    }
    const body = norm((document.body && document.body.textContent) || "").slice(0, 5000);
    if (/are you (a )?human|verify (that )?you.?re (a )?human|verify you are human|i'?m not a robot|complete the captcha|press (and|&) hold|unusual traffic|checking your browser before/.test(body))
      return "captcha";
    const pw = qsaDeep('input[type="password"]').find((x) => !x.getBoundingClientRect || x.getBoundingClientRect().width > 0);
    if (pw) return "auth";
    if (/(please )?(sign in|log ?in) to (apply|continue|view this)|you must (sign in|log ?in|be logged in)|create an account to apply|sign in to your account to continue/.test(body))
      return "auth";
    return "";
  }

  // One-call orchestrator for the desktop shell: fill everything the payload supports and report
  // "filled X of Y" so the person knows exactly what to review. Never clicks submit.
  function fill(payload) { return withRoots(() => fillPass(payload)); }
  function fillPass(payload) {
    // Assisted apply must NEVER run on LinkedIn, browse-by-hand only (CLAUDE.md §7).
    if (/(^|\.)linkedin\.com$/i.test(location.hostname)) {
      return { ok: false, reason: "linkedin" };
    }
    // An AUTH wall means the form isn't reachable yet -- there is nothing to fill behind a
    // login, so stop and say so.
    const blocker = pageBlocker();
    if (blocker === "auth") {
      return { ok: false, reason: "blocked", blocker,
               message: "This page needs you to sign in first. Sign in, then click fill again "
                        + "and I'll take it from there." };
    }
    // A CAPTCHA is different, and treating it like an auth wall was a mistake. The captcha
    // gates SUBMISSION, not the form: typing a name into a text box is not getting past a
    // challenge, and the challenge still stands afterwards for the person to solve. Refusing
    // to fill left them re-typing 66 fields by hand on exactly the pages where the help is
    // worth most -- all of the effort, none of the evasion. So fill, and report the captcha
    // so the caller knows the submit click is unavoidably theirs (CLAUDE.md §7: we never
    // intercept, solve or bypass it).
    if (payload && payload.recipes) setRecipes(payload.recipes);   // fresh table from the app
    const recipe = recipeFor();
    const fields = (payload && payload.fields) || {};
    const plan = planFill(fields, { recipe });
    const filled = applyFill(plan);
    const screening = fillScreening(payload && payload.screening);
    const answers = fillTextAnswers(payload && payload.answers);
    const eeo = (payload && payload.eeo && payload.eeo.decline) ? fillEeo() : 0;
    const resume_upload = hasResumeUpload();
    // Y in "filled X of Y": the fields SponsorJobs RECOGNIZED it could help with (profile fields with a
    // saved value + the screening/text/EEO answers it set + a résumé upload it saw). X = what it set
    // now (a field you'd already typed counts toward Y but not X, so "X of Y" reads honestly).
    const total = plan.length + screening + answers + eeo + (resume_upload ? 1 : 0);
    // The recipe's extras: outline the submit button (the click stays the person's), say when the
    // form continues on another page, and count what is still empty for the model-plan fallback.
    const submit_highlighted = highlightSubmit(recipe);
    const cont = continueButton(recipe);
    const next_page = !!(recipe && recipe.multipage && cont);
    const remaining = extractForm({ onlyEmpty: true }).length;
    return { ok: true, filled: filled + answers, total, screening, answers, eeo, resume_upload,
             via_recipe: plan.filter((p) => p.via === "recipe").length,
             system: recipe ? recipe.id : "", system_name: recipe ? recipe.name : "",
             submit_highlighted, next_page,
             continue_hint: next_page ? "Continue to the next page, then press Fill again." : "",
             handoff: !!(recipe && recipe.handoff), remaining,
             // Present => the site requires a human to clear a challenge before submitting,
             // so the caller must hand the page back rather than click submit itself.
             captcha: blocker === "captcha" };
  }

  // ---------------------------------------------------------- AI-planned fill (autoapply)
  // The autonomous-applier upgrade over the keyword engine above: the desktop shell extracts the
  // form's STRUCTURE with extractForm(), the Python brain plans the fill (the person's real data
  // NEVER reaching the model), and applyOps() executes the returned plan here. Same conservative
  // rules: only EMPTY fields, highlight each, NEVER click submit, drop on the same captcha/auth
  // walls, and a file upload is handed back to the shell (script cannot set a file input; the shell
  // fulfils it via CDP). Reuses every helper above, so there is no second DOM engine.
  let _refN = 0;
  function fieldRef(el) {
    let r = el.getAttribute("data-tailor-ref");
    if (!r) {
      r = _refPrefix + "t" + (_refN++);
      if (typeof el.setAttribute === "function") el.setAttribute("data-tailor-ref", r);
    }
    return r;
  }
  function fieldType(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    const t = (el.type || "text").toLowerCase();
    if (t === "file") return "file";
    if (t === "checkbox") return "checkbox";
    if (t === "radio") return "radio";          // a native radio group: the plan's answer picks an option
    return ["email", "tel", "url", "date", "number"].includes(t) ? t : "text";
  }

  // The interactable fields on the form, STRUCTURE ONLY (labels/types/options, never any value),
  // each with a stable ref the executor targets. This is exactly what goes to the brain.
  // With {onlyEmpty: true} it lists just what is still blank after the recipe + label passes,
  // which is all the model-plan fallback should be asked about.
  function extractForm(opts) {
    const onlyEmpty = !!(opts && opts.onlyEmpty);
    const fields = [];
    qsaDeep("input, textarea, select").forEach((el) => {
      if (el.disabled || el.readOnly) return;
      const raw = el.tagName.toLowerCase() === "input" ? (el.type || "text").toLowerCase() : el.tagName.toLowerCase();
      if (["radio", "hidden", "submit", "button", "reset", "password", "image"].includes(raw)) return;
      if (isWidgetInput(el)) return;                                         // listed below as a widget
      if (siteSearch(el)) return;                                            // the site's job search
      if (onlyEmpty && raw !== "file" && raw !== "checkbox" && String(el.value || "").trim()
          && !/^(select|choose|--|please)/i.test(String(el.value))) return;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0 && raw !== "file") return;   // truly hidden (files can be styled away)
      // A box a person cannot see or reach: react-select's hidden "required" mirror input
      // (aria-hidden, opacity 0), and the classic honeypot. Never offered to the plan.
      if (raw !== "file" && raw !== "checkbox" && (el.getAttribute("aria-hidden") === "true" || !shown(el))) return;
      if (honeypot(el)) return;
      let label = rawLabel(el);
      if (!label && raw !== "file") return;                                  // need a question to answer
      if (raw === "checkbox") label = checkboxLabel(el, label);
      const f = { ref: fieldRef(el), label: (label || "Résumé / CV").slice(0, 200),
                  type: fieldType(el), required: !!(el.required || REQUIRED_MARK.test(label || "")) };
      if (el.tagName.toLowerCase() === "select") {
        f.options = [...el.options].map((o) => (o.textContent || o.value).trim())
          .filter((t) => t && !isPlaceholderText(t)).slice(0, 40);              // keeps "Choose not to disclose"
      }
      fields.push(f);
    });
    for (const radios of radioGroups()) {                                    // one field per radio group
      // The QUESTION, never the first option's own label (Lever and Ashby label each option).
      const q = groupQuestionText(radios[0]);
      if (!q) continue;
      if (onlyEmpty && radios.some((r) => r.checked)) continue;
      const ref = fieldRef(radios[0]);
      radios.forEach((r) => r.setAttribute("data-tailor-group", ref));
      fields.push({ ref, label: q.slice(0, 200), type: "radio",
                    options: radios.map((r) => radioOptionText(r)).filter(Boolean).slice(0, 40), required: false });
    }
    for (const f of widgetFields(onlyEmpty)) fields.push(f);                // custom dropdowns, ARIA radios/checkboxes
    return fields;
  }

  const REQUIRED_MARK = /[*\u2731]\s*$/;
  // The other checkboxes answering the same question (same name, or the same fieldset).
  function checkboxGroup(el) {
    const root = rootOf(el);
    let peers = [];
    if (el.name) peers = qsaSafe('input[type="checkbox"]', root).filter((c) => c.name === el.name);
    if (peers.length < 2) {
      const fs = el.closest("fieldset");
      peers = fs ? qsaSafe('input[type="checkbox"]', fs) : [el];
    }
    return peers;
  }
  // One box of a multi-choice question ("How did you hear about us?": LinkedIn / Referral / ...)
  // is labelled "Question: Option", so the plan and the person see what it answers.
  function checkboxLabel(el, label) {
    if (checkboxGroup(el).length < 2) return label;
    const q = groupQuestionText(el);
    let opt = "";
    if (el.id) { const l = labelFor(el); if (l) opt = labelText(l, el); }
    if (!opt) { const p = el.closest("label"); if (p) opt = labelText(p, el); }
    if (!opt) opt = cleanText(el.getAttribute("aria-label") || el.value || "");
    if (!q || canon(q) === canon(opt)) return opt || label;
    if (!opt || canon(opt) === canon(q)) return q;
    return q + ": " + opt;
  }

  const elByRef = (ref) => qsaDeep(`[data-tailor-ref="${CSS.escape(ref)}"]`)[0] || null;

  // Execute the brain's resolved ops. Returns {filled, uploads:[refs], skipped}. Upload ops are
  // returned for the shell to fulfil via CDP (a file input can't be set from script). Never submits,
  // never overwrites what the person already typed.
  function applyOps(ops) {
    let filled = 0, skipped = 0;
    const uploads = [];
    for (const op of (ops || [])) {
      const el = elByRef(op.ref);
      if (!el) { skipped++; continue; }
      if (op.op === "upload") { uploads.push(op.ref); continue; }
      if (el.getAttribute && el.getAttribute("data-tailor-widget")) { skipped++; continue; }   // applyOpsAsync
      const type = fieldType(el);
      const text = String(op.text == null ? "" : op.text);
      try {
        if (type === "radio") {
          const radios = qsaDeep(`[data-tailor-group="${CSS.escape(op.ref)}"]`);
          if (pickRadio(radios, (t) => norm(t) === norm(text) || norm(t).startsWith(norm(text)))) filled++; else skipped++;
        } else if (type === "select") {
          const cur = norm(el.value);
          if (cur && !/^(select|choose|--|please)/.test(cur)) { skipped++; continue; }
          if (fillSelect(el, text)) { markFilled(el); filled++; } else skipped++;
        } else if (type === "checkbox") {
          if (!el.checked && /^(yes|true|1|checked|on)$/i.test(text)) { setChecked(el); markFilled(el); filled++; } else skipped++;
        } else {
          if (String(el.value || "").trim()) { skipped++; continue; }        // never overwrite typed text
          setNativeValue(el, text); markFilled(el); filled++;
        }
      } catch (e) { skipped++; }
    }
    return { filled, uploads, skipped };
  }

  // ---------------------------------------------------------- custom widgets
  // Most current ATS forms draw their own dropdowns, radio groups and checkboxes instead of
  // using <select>/<input type=radio>: react-select on Greenhouse's new boards, Workday's
  // listbox and prompt widgets (data-automation-id), Ashby/Lever custom selects, and plain ARIA
  // role=combobox / role=radio / role=checkbox. The native passes above never see them. This
  // layer drives them the way a keyboard-and-mouse user does: open the menu, type to filter,
  // pick the best matching option, then CHECK that the choice stuck (a click a widget ignored is
  // reported as left for the person, never counted as filled).
  // Waits are fixed polling steps while a menu renders, never randomized: nothing here imitates
  // a human or hides from bot detection (CLAUDE.md section 7). Submit / Next are never touched.
  // A widget's prompt text ("Select One", "-- Choose --", "Search..."), never a chosen value. Only
  // a pure prompt: a chosen "Choose not to disclose" (a decline) or "Search Engine (Google, Bing)"
  // is an answer. The same rule as agent_view.js and autoapply/asks.py.
  const DECLINE_TEXT_RE = /decline|prefer not|do not wish|don['’]?t wish|not wish to|(?:do not|don['’]?t) want to (?:answer|disclose|say|self[- ]?identify|provide)|choose not|rather not|not to (?:say|answer|disclose)/i;
  const PROMPT_TEXT_RE = /^[\s\-_*.\u2013\u2014]*(?:(?:please )?(?:select|choose)\b|please (?:enter|type|pick)\b|make a selection\b)|^-{2,}|^(?:none selected|no selection|nothing selected)\W*$|^(?:search|start typing|type to search)(?:\W*$|\s*(?:\.{3}|\u2026)|\s+(?:for|by|here)\b)/i;
  function isPlaceholderText(t) {
    const s = norm(t);
    return !!s && PROMPT_TEXT_RE.test(s) && !DECLINE_TEXT_RE.test(s);
  }
  const COMBO_SEL = "[role='combobox'], button[aria-haspopup='listbox'], [data-uxi-widget-type='selectinput']";
  const OPTION_SEL = "[role='option'], [id*='-option-'], [data-automation-id='promptOption'], [data-automation-id='promptLeafNode']";
  const VALUE_SEL = "[class*='singleValue'], [class*='single-value'], [class*='multiValue__label'], "
                  + "[class*='multi-value__label'], [data-automation-id='selectedItem']";
  const VALUE_BOX_SEL = "[class*='value-container'], [class*='ValueContainer']";
  const LOADING_MAX_MS = 6000;
  const NOT_AN_OPTION = /^(no options|no results|loading|searching|no matches|type to search|start typing)/;
  const WIDGET_WAIT_MS = 2500;
  const STEP_MS = 50;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function viewOf(el) {
    try { return (el.ownerDocument && el.ownerDocument.defaultView) || window; } catch (e) { return window; }
  }
  // Dispatch a plain DOM event of the element's own realm (frames have their own constructors).
  function fire(el, type, init) {
    const v = viewOf(el);
    const C = /^(mouse|click|pointer)/.test(type) ? (v.MouseEvent || MouseEvent)
            : /^key/.test(type) ? (v.KeyboardEvent || KeyboardEvent) : (v.Event || Event);
    try {
      el.dispatchEvent(new C(type, Object.assign({ bubbles: true, cancelable: true, composed: true }, init || {})));
    } catch (e) { /* a detached node */ }
  }
  function press(el) {
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) fire(el, t, { button: 0 });
  }
  function boxShown(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect() || {};
    if (!r.width && !r.height) return false;
    try {
      const cs = viewOf(el).getComputedStyle(el);
      if (cs && (cs.display === "none" || cs.visibility === "hidden")) return false;
    } catch (e) { /* no styles */ }
    return true;
  }
  const tagOf = (el) => String((el && el.tagName) || "").toUpperCase();
  const canon = (s) => String(s || "").toLowerCase().replace(/\./g, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim();

  function otherCombo(node, el) {
    return qsaSafe(COMBO_SEL, node).some((o) => o !== el && !o.contains(el) && !el.contains(o));
  }
  // The visible control a person clicks (react-select's control box, else the element itself).
  function controlOf(el) {
    let node = el;
    for (let i = 0; i < 4 && node && node.parentElement; i++) {
      node = node.parentElement;
      if (/control/i.test(String(node.className || ""))) return node;
    }
    return el;
  }

  function comboboxes() {
    const out = [];
    for (const el of qsaDeep(COMBO_SEL)) {
      const tag = tagOf(el);
      if (tag === "SELECT" || out.includes(el)) continue;
      if (el.getAttribute("aria-disabled") === "true" || el.disabled) continue;
      if (siteSearch(el)) continue;                        // a typeahead job search, not a question
      // ARIA 1.1: a wrapper says role=combobox and holds the real text input. Use the input.
      if (tag !== "INPUT" && tag !== "BUTTON" && qsaSafe("input", el).some((i) => String(i.type || "text") !== "hidden")) continue;
      if (!boxShown(el) && !boxShown(controlOf(el))) continue;
      out.push(el);
    }
    return out;
  }

  // What the widget shows as chosen now ("" = nothing chosen yet).
  function comboValue(el) {
    if (tagOf(el) !== "INPUT") {
      const t = norm(el.textContent);
      return isPlaceholderText(t) ? "" : t;
    }
    let node = el;
    for (let i = 0; i < 5 && node.parentElement; i++) {
      node = node.parentElement;
      if (otherCombo(node, el)) break;
      const v = qsaSafe(VALUE_SEL, node).find((x) => norm(x.textContent));
      if (v) return norm(v.textContent);
      // A select-style box with no chosen value: its search text is NOT an answer (react-select
      // hides the placeholder while you type and drops the text on blur).
      if (qsaSafe("[class*='placeholder'], " + VALUE_BOX_SEL, node).length) return "";
    }
    return el.getAttribute("aria-expanded") === "true" ? "" : norm(el.value);
  }

  // Does this input sit in a select-style control that shows a chosen value (react-select)?
  function hasValueBox(el) {
    let node = el;
    for (let i = 0; i < 5 && node.parentElement; i++) {
      node = node.parentElement;
      if (otherCombo(node, el)) break;
      if (qsaSafe(VALUE_SEL + ", [class*='placeholder'], " + VALUE_BOX_SEL, node).length) return true;
    }
    return false;
  }
  function isWidgetInput(el) {
    return !!(el && el.getAttribute && (el.getAttribute("role") === "combobox"
      || el.getAttribute("data-uxi-widget-type") === "selectinput"));
  }

  function cleanLabel(t) {
    return String(t || "").replace(/\s+/g, " ").replace(/\b(select one|required)\b/ig, "").replace(/[*\u2731]+/g, "").trim();
  }
  function widgetLabel(el) {
    return cleanLabel(rawLabel(el) || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "");
  }

  function listboxFor(el) {
    for (const attr of ["aria-controls", "aria-owns"]) {
      for (const id of String(el.getAttribute(attr) || "").split(/\s+/).filter(Boolean)) {
        const n = byId(el, id); if (n) return n;
      }
    }
    let node = el;
    for (let i = 0; i < 6 && node.parentElement; i++) {     // react-select: the menu sits beside the control
      node = node.parentElement;
      if (otherCombo(node, el)) break;
      const m = qsaSafe("[role='listbox'], [class*='menu']", node).find((x) => !x.contains(el) && boxShown(x));
      if (m) return m;
    }
    // A menu portalled to the end of <body> (Workday, react-select menuPortalTarget): the
    // visible listbox, newest last.
    const lbs = qsaDeep("[role='listbox']").filter((x) => boxShown(x) && !x.contains(el));
    return lbs.length ? lbs[lbs.length - 1] : null;
  }
  function optionText(o) { return norm(o.textContent || o.getAttribute("aria-label") || ""); }
  function optionsFor(el) {
    const lb = listboxFor(el);
    if (!lb) return [];
    let opts = qsaSafe(OPTION_SEL, lb).filter(boxShown);
    if (!opts.length) opts = qsaSafe("[class*='option']", lb).filter(boxShown);
    opts = opts.filter((o) => !opts.some((p) => p !== o && o.contains(p)));   // innermost: its click bubbles up
    return opts.filter((o) => o.getAttribute("aria-disabled") !== "true" && optionText(o)
                              && !NOT_AN_OPTION.test(optionText(o)));
  }
  function menuOpen(el) {
    return el.getAttribute("aria-expanded") === "true" || optionsFor(el).length > 0;
  }
  async function openCombo(el) {
    try { if (el.focus) el.focus(); } catch (e) { /* fine */ }
    if (menuOpen(el)) return true;
    fire(el, "pointerdown", { button: 0 }); fire(el, "mousedown", { button: 0 });
    await sleep(STEP_MS);
    if (menuOpen(el)) return true;
    fire(el, "mouseup", { button: 0 }); fire(el, "click", { button: 0 });
    await sleep(STEP_MS);
    if (menuOpen(el)) return true;
    fire(el, "keydown", { key: "ArrowDown", code: "ArrowDown", keyCode: 40, which: 40 });
    await sleep(STEP_MS);
    return menuOpen(el);
  }
  async function closeCombo(el) {
    if (el.getAttribute("aria-expanded") === "true" || optionsFor(el).length) {
      fire(el, "keydown", { key: "Escape", code: "Escape", keyCode: 27, which: 27 });
      await sleep(STEP_MS);
    }
    try { if (el.blur) el.blur(); } catch (e) { /* fine */ }
  }

  // ---- matching an option to the value we want
  const SYNONYMS = [
    ["united states", "united states of america", "usa", "us", "america", "u s a", "u s"],
    ["united kingdom", "uk", "great britain", "gb", "england"],
    ["united arab emirates", "uae"],
    ["south korea", "republic of korea", "korea republic of", "korea"],
  ];
  const US_STATES = {al:"alabama",ak:"alaska",az:"arizona",ar:"arkansas",ca:"california",co:"colorado",
    ct:"connecticut",de:"delaware",dc:"district of columbia",fl:"florida",ga:"georgia",hi:"hawaii",id:"idaho",
    il:"illinois",in:"indiana",ia:"iowa",ks:"kansas",ky:"kentucky",la:"louisiana",me:"maine",md:"maryland",
    ma:"massachusetts",mi:"michigan",mn:"minnesota",ms:"mississippi",mo:"missouri",mt:"montana",ne:"nebraska",
    nv:"nevada",nh:"new hampshire",nj:"new jersey",nm:"new mexico",ny:"new york",nc:"north carolina",
    nd:"north dakota",oh:"ohio",ok:"oklahoma",or:"oregon",pa:"pennsylvania",ri:"rhode island",
    sc:"south carolina",sd:"south dakota",tn:"tennessee",tx:"texas",ut:"utah",vt:"vermont",va:"virginia",
    wa:"washington",wv:"west virginia",wi:"wisconsin",wy:"wyoming"};
  function variants(value) {
    const c = canon(value);
    const out = new Set(c ? [c] : []);
    for (const g of SYNONYMS) if (g.includes(c)) g.forEach((x) => out.add(x));
    if (US_STATES[c]) out.add(US_STATES[c]);
    for (const k of Object.keys(US_STATES)) if (US_STATES[k] === c) out.add(k);
    return [...out];
  }
  const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // Score an option's text for a wanted value: exact > starts with > is a prefix of > contains
  // as whole words > shares the first part ("Chicago" of "Chicago, IL"). Shorter wins a tie.
  function valueScorer(value) {
    const vs = variants(value);
    const first = canon(String(value || "").split(",")[0]);
    const rest = String(value || "").split(",").slice(1).map((x) => x.trim()).filter(Boolean);
    return (text) => {
      const t = canon(text);
      if (!t) return 0;
      let s = 0;
      for (const v of vs) {
        if (!v) continue;
        if (t === v) s = Math.max(s, 100);
        else if (new RegExp("^" + reEsc(v) + "\\b").test(t)) s = Math.max(s, 80);
        else if (t.length >= 4 && new RegExp("^" + reEsc(t) + "\\b").test(v)) s = Math.max(s, 70);
        else if (v.length >= 3 && new RegExp("\\b" + reEsc(v) + "\\b").test(t)) s = Math.max(s, 60);
      }
      if (!s && first && first.length >= 3 && new RegExp("\\b" + reEsc(first) + "\\b").test(t)) {
        s = 40;
        // "Chicago, IL": the state (or country) after the comma breaks a tie between Chicagos.
        for (const part of rest) {
          if (variants(part).some((v) => v.length >= 2 && new RegExp("\\b" + reEsc(v) + "\\b").test(t))) s += 15;
        }
      }
      return s ? s - Math.min(9, t.length / 20) : 0;
    };
  }
  function regexScorer(re) {
    return (text) => (re.test(norm(text)) ? 50 - Math.min(9, norm(text).length / 20) : 0);
  }
  function bestOf(opts, score) {
    let best = null;
    for (const o of opts) {
      const s = score(optionText(o));
      if (s > 0 && (!best || s > best.score)) best = { opt: o, score: s };
    }
    return best;
  }
  // Is the widget still fetching its options (react-select's loading indicator, a "Loading..."
  // or "Searching..." notice, aria-busy)? A slow location search must not be read as "no match".
  function loading(el) {
    const lb = listboxFor(el);
    if (lb && (/^(loading|searching)/.test(norm(lb.textContent))
               || qsaSafe("[class*='loading'], [aria-busy='true']", lb).some(boxShown))) return true;
    let node = el;
    for (let i = 0; i < 5 && node.parentElement; i++) {
      node = node.parentElement;
      if (otherCombo(node, el)) break;
      if (qsaSafe("[class*='loading-indicator'], [class*='loadingIndicator'], [aria-busy='true']", node).some(boxShown)) return true;
    }
    return false;
  }
  async function waitBest(el, score, ms) {
    let until = Date.now() + ms;
    const hard = until + LOADING_MAX_MS;
    let lastN = -1;
    while (Date.now() < until) {
      const opts = optionsFor(el);
      const best = bestOf(opts, score);
      const busy = loading(el);
      if (best && !busy && (best.score >= 99 || opts.length === lastN)) return best;
      if (busy) until = Math.min(hard, Math.max(until, Date.now() + 300));
      lastN = opts.length;
      await sleep(STEP_MS);
    }
    return bestOf(optionsFor(el), score);
  }
  function sameChoice(v, label) {
    const a = canon(v), b = canon(label);
    return !!a && !!b && (a === b || a.includes(b) || b.includes(a));
  }
  function typeQueries(text) {
    const out = [];
    for (const q of [String(text || "").trim(), String(text || "").split(",")[0].trim()]) {
      if (q && !out.includes(q)) out.push(q);
    }
    return out;
  }

  // Open a combobox, find the best option (typing to filter when the list is long or loads as
  // you type), click it, and verify. Returns {ok, value} or {ok:false, why}.
  async function pickCombo(el, want) {
    if (comboValue(el)) return { ok: false, why: "answered" };
    const isInput = tagOf(el) === "INPUT";
    await openCombo(el);
    let best = await waitBest(el, want.score, isInput && want.text ? 400 : WIDGET_WAIT_MS);
    if (!best && isInput && want.text) {
      for (const q of typeQueries(want.text)) {
        setNativeValue(el, q);
        best = await waitBest(el, want.score, WIDGET_WAIT_MS);
        if (best) break;
      }
    }
    if (!best && isInput && want.freeText && want.text && !hasValueBox(el)) {
      // A plain type-ahead text box (an address autocomplete): no list to pick from, so the
      // typed value IS the answer, the way a person would leave it.
      if (canon(el.value) !== canon(want.text)) setNativeValue(el, want.text);
      await closeCombo(el);
      return { ok: canon(el.value) === canon(want.text), value: want.text };
    }
    if (!best) {
      if (isInput && el.value) setNativeValue(el, "");        // leave no stray filter text behind
      await closeCombo(el);
      return { ok: false, why: "no-match" };
    }
    const label = optionText(best.opt);
    try { if (best.opt.scrollIntoView) best.opt.scrollIntoView({ block: "nearest" }); } catch (e) { /* fine */ }
    fire(best.opt, "mouseover"); fire(best.opt, "mousemove");
    press(best.opt);
    await sleep(STEP_MS * 2);
    let v = comboValue(el);
    if (!sameChoice(v, label) && menuOpen(el) && isInput) {
      // A menu that commits on Enter (keyboard pattern). Only while the menu is open, where
      // Enter means "choose the highlighted option", never "send the form".
      fire(el, "keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13 });
      await sleep(STEP_MS * 2);
      v = comboValue(el);
    }
    await closeCombo(el);
    v = comboValue(el);
    return sameChoice(v, label) ? { ok: true, value: label } : { ok: false, why: "did-not-stick" };
  }

  // ---- ARIA radio groups (and Yes/No button pairs with aria-pressed), ARIA checkboxes
  function ariaRadioGroups() {
    const groups = new Map();
    for (const r of qsaDeep("[role='radio'], button[aria-pressed]")) {
      if (r.getAttribute("aria-disabled") === "true" || r.disabled || !boxShown(r)) continue;
      const box = (r.closest && r.closest("[role='radiogroup']")) || r.parentElement;
      if (!box) continue;
      if (!groups.has(box)) groups.set(box, []);
      groups.get(box).push(r);
    }
    const out = [];
    for (const [box, radios] of groups) {
      if (radios.length < 2 && box.getAttribute("role") !== "radiogroup") continue;
      out.push({ box, radios });
    }
    return out;
  }
  const isOn = (r) => r.getAttribute("aria-checked") === "true" || r.getAttribute("aria-pressed") === "true";
  // The group's question in its own case (for the plan); route with norm(widgetQuestion(box)).
  function widgetQuestion(box) {
    const t = ariaLabelledText(box) || box.getAttribute("aria-label") || "";
    if (t) return cleanLabel(t);
    return cleanLabel(groupQuestionText(box));
  }
  const rawText = (o) => String(o.textContent || o.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
  async function pickAriaRadio(radios, score) {
    if (radios.some(isOn)) return false;
    const best = bestOf(radios, score);
    if (!best) return false;
    press(best.opt);
    for (let i = 0; i < 10 && !isOn(best.opt); i++) await sleep(STEP_MS);
    if (!isOn(best.opt)) {                  // the keyboard way: Space selects a focused radio
      try { if (best.opt.focus) best.opt.focus(); } catch (e) { /* fine */ }
      fire(best.opt, "keydown", { key: " ", code: "Space", keyCode: 32, which: 32 });
      await sleep(STEP_MS * 2);
    }
    if (isOn(best.opt)) { markFilled(best.opt); return true; }
    return false;
  }
  function ariaCheckboxes() {
    return qsaDeep("[role='checkbox'], [role='switch']").filter((c) =>
      c.getAttribute("aria-disabled") !== "true" && tagOf(c) !== "INPUT" && boxShown(c));
  }
  async function toggleAria(el) {
    if (el.getAttribute("aria-checked") === "true") return false;
    press(el);
    for (let i = 0; i < 10 && el.getAttribute("aria-checked") !== "true"; i++) await sleep(STEP_MS);
    return el.getAttribute("aria-checked") === "true";
  }

  // The widget fields for extractForm: a combobox is a "select", an ARIA radio group a "radio",
  // an ARIA checkbox a "checkbox", in the same plan format as the native fields.
  const _comboOptions = new WeakMap();
  function widgetFields(onlyEmpty) {
    const fields = [];
    for (const el of comboboxes()) {
      const label = widgetLabel(el);
      if (!label) continue;
      if (onlyEmpty && comboValue(el)) continue;
      const ref = fieldRef(el);
      el.setAttribute("data-tailor-widget", "combobox");
      fields.push({ ref, label: label.slice(0, 200), type: "select", widget: "combobox",
                    options: (_comboOptions.get(el) || []).slice(0, 40),
                    required: el.getAttribute("aria-required") === "true" || REQUIRED_MARK.test(rawLabel(el)) });
    }
    for (const g of ariaRadioGroups()) {
      const q = widgetQuestion(g.box);
      if (!q) continue;
      if (onlyEmpty && g.radios.some(isOn)) continue;
      const ref = fieldRef(g.box);
      g.box.setAttribute("data-tailor-widget", "radio");
      g.radios.forEach((r) => r.setAttribute("data-tailor-group", ref));
      fields.push({ ref, label: q.slice(0, 200), type: "radio", widget: "radio",
                    options: g.radios.map(rawText).filter(Boolean).slice(0, 40),
                    required: g.box.getAttribute("aria-required") === "true" });
    }
    for (const c of ariaCheckboxes()) {
      const label = cleanLabel(ariaLabelledText(c) || c.getAttribute("aria-label") || c.textContent || rawLabel(c));
      if (!label) continue;
      if (onlyEmpty && c.getAttribute("aria-checked") === "true") continue;
      const ref = fieldRef(c);
      c.setAttribute("data-tailor-widget", "checkbox");
      fields.push({ ref, label: label.slice(0, 200), type: "checkbox", widget: "checkbox",
                    required: c.getAttribute("aria-required") === "true" });
    }
    return fields;
  }

  // extractForm, plus the option lists of custom dropdowns, which only exist in the page while
  // the menu is open: open each (at most 15), read up to 40 options, close it again.
  async function extractFormAsync(opts) {
    let n = 0;
    for (const el of comboboxes()) {
      if (n >= 15) break;
      if (_comboOptions.has(el) || comboValue(el)) continue;
      n++;
      await openCombo(el);
      const until = Date.now() + 600;
      let opts2 = optionsFor(el), last = -1;
      while (Date.now() < until && (opts2.length === 0 || opts2.length !== last)) {
        last = opts2.length; await sleep(STEP_MS); opts2 = optionsFor(el);
      }
      _comboOptions.set(el, opts2.map((o) => (o.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean).slice(0, 40));
      await closeCombo(el);
    }
    return extractForm(opts);
  }

  // Screening / EEO / contact values for the custom widgets, by the same rules as the native
  // passes: saved Yes/No answers, the EEO decline the person opted into, and profile values
  // (country, state, city, location). Anything else is left for the model plan or the person.
  async function fillWidgets(payload) {
    const out = { filled: 0, fields: 0, screening: 0, eeo: 0 };
    if (!payload) return out;
    const f = derivedFields(payload.fields || {});
    const rules = screenRules();
    const { topics, decline } = eeoRes();
    const route = (q) => {
      const hits = rules.filter((s) => s.re.test(q));
      if (hits.length > 1) return null;                    // combined question: polarity unclear
      if (hits.length === 1) {
        const a = screeningAnswer(payload.screening, hits[0].key, q);
        return a ? { kind: "screening", score: regexScorer(a === "Yes" ? YES_RE : NO_RE) } : null;
      }
      if (topics.test(q)) {
        return (payload.eeo && payload.eeo.decline) ? { kind: "eeo", score: regexScorer(decline) } : null;
      }
      return undefined;
    };
    for (const el of comboboxes()) {
      if (comboValue(el)) continue;
      const q = norm(widgetLabel(el));
      let r = route(q);
      if (r === null) continue;
      if (r === undefined) {
        let value = "";
        if (/\blocation\b/.test(q)) value = f.location;
        else {
          const key = categorize(el, signature(el) || q);
          if (key) value = f[key];
        }
        if (!value) continue;
        r = { kind: "fields", text: value, score: valueScorer(value), freeText: true };
      }
      const res = await pickCombo(el, r);
      if (res.ok) { out[r.kind]++; out.filled++; markFilled(controlOf(el)); }
    }
    for (const g of ariaRadioGroups()) {
      if (g.radios.some(isOn)) continue;
      const r = route(norm(widgetQuestion(g.box)));
      if (!r) continue;
      if (await pickAriaRadio(g.radios, r.score)) { out[r.kind]++; out.filled++; }
    }
    return out;
  }

  // One op of the model plan on a custom widget. "filled" | "skipped".
  async function applyWidgetOp(el, op) {
    const kind = el.getAttribute("data-tailor-widget");
    const text = String(op.text == null ? "" : op.text);
    if (kind === "combobox") {
      if (!text) return "skipped";
      const r = await pickCombo(el, { text, score: valueScorer(text) });
      if (r.ok) { markFilled(controlOf(el)); return "filled"; }
      return "skipped";
    }
    if (kind === "radio") {
      const radios = qsaDeep(`[data-tailor-group="${CSS.escape(op.ref)}"]`);
      return (await pickAriaRadio(radios, valueScorer(text))) ? "filled" : "skipped";
    }
    if (kind === "checkbox") {
      if (!/^(yes|true|1|checked|on)$/i.test(text)) return "skipped";
      if (await toggleAria(el)) { markFilled(el); return "filled"; }
      return "skipped";
    }
    return "skipped";
  }

  // applyOps for the shell: native fields as before, custom widgets driven and verified.
  async function applyOpsAsync(ops) {
    const native = [], widget = [];
    for (const op of (ops || [])) {
      const el = elByRef(op.ref);
      if (el && op.op !== "upload" && el.getAttribute("data-tailor-widget")) widget.push([el, op]);
      else native.push(op);
    }
    const rep = applyOps(native);
    for (const [el, op] of widget) {
      let res = "skipped";
      try { res = await applyWidgetOp(el, op); } catch (e) { res = "skipped"; }
      if (res === "filled") rep.filled++; else rep.skipped++;
    }
    return rep;
  }

  // The keyword fill plus the custom widgets: what the desktop shell runs.
  async function fillAsync(payload) {
    const r = fill(payload);
    if (!r || !r.ok) return r;
    let w = { filled: 0, fields: 0, screening: 0, eeo: 0 };
    try { w = await fillWidgets(payload); } catch (e) { /* a widget threw: the rest stands */ }
    r.filled += w.fields;
    r.screening += w.screening;
    r.eeo += w.eeo;
    r.total += w.filled;
    r.widgets = w.filled;
    r.remaining = extractForm({ onlyEmpty: true }).length;
    return r;
  }

  // What is still blank on this page and needs the person, by label: required checkboxes only
  // (an optional newsletter box is not "left"), a file input only while no file is attached.
  function leftover() {
    const out = [];
    for (const f of extractForm({ onlyEmpty: true })) {
      const el = elByRef(f.ref);
      if (f.type === "file") { if (el && el.files && el.files.length) continue; }
      else if (f.type === "checkbox") {
        if (!f.required) continue;
        if (el && (el.checked || el.getAttribute("aria-checked") === "true")) continue;
        if (el && tagOf(el) === "INPUT") {
          const peers = checkboxGroup(el);
          if (peers.some((c) => c.checked)) continue;
          if (peers.length > 1) {
            const q = cleanLabel(groupQuestionText(el));
            if (q) { if (!out.includes(q)) out.push(q); continue; }
          }
        }
      }
      const label = cleanLabel(f.label);
      if (label && !out.includes(label)) out.push(label);
    }
    return out;
  }

  // A cheap fingerprint of the form on screen, for spotting "the person pressed Next and a new
  // page of questions appeared" (URL changes alone miss single-page apps like Workday).
  function pageSignature() {
    return withRoots(() => {
      const all = extractForm({});
      return { labels: all.map((f) => cleanLabel(f.label)).filter(Boolean).slice(0, 200),
               empty: extractForm({ onlyEmpty: true }).length,
               blocker: pageBlocker(), success: !!detectSuccess(), url: hereUrl(),
               cross_frames: crossOriginFrames() };
    });
  }

  window.TailorAutofill = {
    fill, pageBlocker, applicationForm, applyButtonLabel, siteSearch, planFill, applyFill, fillScreening, fillTextAnswers, answerKeyForLabel,
    fillEeo, hasResumeUpload, candidates, categorize, signature, setNativeValue, markFilled,
    rawLabel, answerableTextareas, pageJobContext, screeningAnswer,
    extractForm, applyOps, fieldRef,                                         // AI-planned autoapply
    // deep DOM + custom widgets + multi-page (desktop shell)
    fillAsync, fillWidgets, extractFormAsync, applyOpsAsync, leftover, pageSignature, setRefPrefix,
    crossOriginFrames, comboboxes, comboValue, pickCombo, valueScorer,
    // recipes: the per-system table, success detection, continue hints, corrections
    setRecipes, recipes: () => RECIPES, recipeFor, globToRe, highlightSubmit, continueButton,
    detectSuccess, watchSuccess, stopSuccessWatch, watchCorrections, takeCorrections,
  };
})();
