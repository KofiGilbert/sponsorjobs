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
  const RECIPES_FALLBACK = /*RECIPES_JSON_START*/{"version":1,"updated":"2026-10-11","about":"Fill recipes for assisted apply (CLAUDE.md section 7). Each system is DATA: URL patterns, ordered CSS selectors per profile key, the continue/next buttons (shown as a hint, never clicked), the submit buttons (HIGHLIGHTED for the person, never clicked), the application-received markers, and notes. Served at GET /api/autofill/recipes and embedded in extension/autofill_core.js as the offline fallback copy (scripts/embed_recipes.py keeps them in sync). Verified by fetching each system's live public page with curl on the date in each entry's 'verified' block; where a system renders its form only with JavaScript the raw HTML is recorded as seen and selectors come from the vendor's own shipped bundle or documented attributes, never guessed.","profile_keys":["first_name","last_name","name","email","phone","address","street","city","state","zip","country","location","linkedin","github","website","resume"],"question_patterns":{"work_auth_us":{"label_re":"authoriz(ed|ation) to work|legally (able|authorized|permitted|eligible) to work|eligible to work|right to work|work authoriz|lawfully (able|authorized)","answer_from":"work_auth_us","answers":["Yes","No"]},"needs_sponsorship":{"label_re":"sponsor|require.*visa|visa.*(status|sponsor|support)|employment visa|immigration (support|sponsor)","future_re":"now or in the future|in the future|will you (ever|at any time)|future","answer_from":"needs_sponsorship","answers":["Yes","No","Future"]},"eeo_decline":{"label_re":"gender|\\brace\\b|ethnic|veteran|disab|sexual orientation|pronoun","option_re":"decline|prefer not|not to (answer|identify|disclose|say)|don'?t (wish|want)|do not (wish|want)|i do not","answer_from":"eeo_decline"}},"systems":[{"id":"greenhouse","name":"Greenhouse","url_patterns":["https://job-boards.greenhouse.io/*","https://boards.greenhouse.io/*","https://job-boards.eu.greenhouse.io/*","https://boards.eu.greenhouse.io/*"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML","urls":["https://job-boards.greenhouse.io/anthropic/jobs/5183044008","https://boards.greenhouse.io/embed/job_app?for=anthropic&token=5183044008"]},"form":"form#application-form","fields":{"first_name":["#first_name","input[autocomplete='given-name']"],"last_name":["#last_name","input[autocomplete='family-name']"],"email":["#email","input[autocomplete='email']"],"phone":["#phone","input[type='tel']"],"country":["#country"],"location":["#candidate-location","#job_application_location","#auto_complete_input"],"linkedin":["input[aria-label*='LinkedIn' i]"],"website":["input[aria-label*='Website' i]","input[aria-label*='Portfolio' i]"],"resume":["#resume","input[type='file'][accept*='.pdf']"]},"continue_buttons":[],"submit_buttons":["form#application-form button[type='submit']","#submit_app","input[type='submit'][value*='Submit' i]"],"success_markers":[{"text":"Thank you for applying"},{"selector":"#application_confirmation"},{"text":"Your application has been submitted"}],"multipage":false,"notes":"boards.greenhouse.io now redirects to job-boards.greenhouse.io; both serve the same server-rendered form (#application-form with #first_name, #last_name, #email, #phone, #country react-select combobox, #resume file input, custom questions as #question_<id>). Submit is 'Submit application' (button[type=submit]). Custom and EEO questions are react-select comboboxes keyed by question id, so they fall through to the label matcher. Captcha-free on the pages read; some boards add reCAPTCHA at submit, which is the person's to solve. Re-read 2026-10-11: #first_name, #last_name, #email, #phone (type=tel), #resume (file), #country (react-select input) and the single button[type=submit] are present; this board has no LinkedIn/website input, where a board asks for them they are custom questions (#question_<id>) the label matcher reads from their <label for>."},{"id":"lever","name":"Lever","url_patterns":["https://jobs.lever.co/*","https://jobs.eu.lever.co/*"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML","urls":["https://jobs.lever.co/abovelending/0adfabd0-e18b-495e-add5-52a2521043dd/apply"]},"form":"form#application-form","fields":{"name":["input[name='name']","input[data-qa='name-input']"],"email":["input[name='email']","input[data-qa='email-input']"],"phone":["input[name='phone']","input[data-qa='phone-input']"],"location":["#location-input","input[name='location']","input[data-qa='location-input']"],"linkedin":["input[name='urls[LinkedIn]']"],"github":["input[name='urls[GitHub]']"],"website":["input[name='urls[Website]']","input[name='urls[Portfolio]']"],"resume":["#resume-upload-input","input[data-qa='input-resume']","input[name='resume']"]},"continue_buttons":[],"submit_buttons":["#btn-submit","button[data-qa='btn-submit']"],"success_markers":[{"url_re":"/thanks(\\?|$)"},{"text":"Application submitted"},{"text":"Thank you for your interest"}],"multipage":false,"notes":"Server-rendered single form (form#application-form). Fields carry data-qa hooks (name-input, email-input, phone-input, location-input, org-input, input-resume, btn-submit). Current company is input[name='org']. Custom questions are cards[<uuid>][field<n>] radios/selects, answered by the label matcher. hCaptcha is on the form (#hcaptchaSubmitBtn, hcaptchaResponse): the submit click is always the person's. After submit Lever navigates to .../thanks."},{"id":"ashby","name":"Ashby","url_patterns":["https://jobs.ashbyhq.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell only: <div id='root'>, Vite bundle); field paths from Ashby's Posting API application-form schema (_systemfield_*)","urls":["https://jobs.ashbyhq.com/assured-health/f373ebcc-def6-47e3-94cc-882e48bf7d2c/application"]},"form":"form","fields":{"name":["#_systemfield_name","input[name='_systemfield_name']"],"email":["#_systemfield_email","input[name='_systemfield_email']"],"phone":["#_systemfield_phone","input[name='_systemfield_phone']"],"location":["#_systemfield_location","input[name='_systemfield_location']"],"linkedin":["input[name*='linkedin' i]","input[id*='linkedin' i]"],"resume":["#_systemfield_resume","input[name='_systemfield_resume']","input[type='file']"]},"continue_buttons":[],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"Application submitted"},{"text":"Your application has been received"}],"multipage":false,"notes":"The raw HTML is an empty React root; the application form renders from the Posting API's application-form definition, whose system fields are keyed _systemfield_name, _systemfield_email, _systemfield_phone, _systemfield_location, _systemfield_resume. The DOM attribute placement (id/name) is not visible to curl, so every field also falls through to the label matcher. The 'Submit application' button is the only type=submit button on the page."},{"id":"workable","name":"Workable","url_patterns":["https://apply.workable.com/*","https://*.workable.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell, rspack bundle) + the live candidate bundle's form configuration (ids firstname, lastname, email, phone, headline, summary, resume, cover_letter, address, photo) and data-ui hooks","urls":["https://apply.workable.com/tiger-analytics/j/39D0D71D6D/apply","https://dcvxs6ggqztsa.cloudfront.net/candidate/releases/careers.9bfb2ee0b8c70361.js"]},"form":"form","fields":{"first_name":["input[name='firstname']","#firstname","[data-ui='firstname'] input"],"last_name":["input[name='lastname']","#lastname","[data-ui='lastname'] input"],"email":["input[name='email']","#email","[data-ui='email'] input"],"phone":["input[name='phone']","#phone","[data-ui='phone'] input"],"address":["input[name='address']","#address","[data-ui='address'] input"],"resume":["[data-ui='resume'] input[type='file']","input[name='resume']","input[type='file']"]},"continue_buttons":[],"submit_buttons":["button[data-ui='submit-application']","button[data-ui='submit-eeoc']","button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"selector":"[data-ui='eeoc-form']"}],"multipage":false,"notes":"The page is a JS shell (window.careers config, rspack bundle; recaptcha and turnstile toggles in config). The bundle's form definition names the standard fields firstname, lastname, email, phone, headline, summary, resume, cover_letter, address, photo; the EEOC survey after submission is [data-ui='eeoc-form'] with submit-eeoc / skip-eeoc buttons, so its appearance also marks the application as received. The exact submit button data-ui was not visible in the raw bundle; type=submit is the fallback."},{"id":"smartrecruiters","name":"SmartRecruiters","url_patterns":["https://jobs.smartrecruiters.com/*","https://careers.smartrecruiters.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of the job page (server-rendered; apply link a.js-oneclick to /oneclick-ui/company/<co>/publication/<uuid>); the oneclick-ui apply page returned 403 to curl","urls":["https://jobs.smartrecruiters.com/abbvie/3743990016045386"]},"form":"form","fields":{"first_name":["input[name='firstName']","#first-name","input[autocomplete='given-name']"],"last_name":["input[name='lastName']","#last-name","input[autocomplete='family-name']"],"email":["input[name='email']","#email","input[type='email']"],"phone":["input[name='phoneNumber']","#phone-number","input[type='tel']"],"resume":["input[type='file']"]},"continue_buttons":["button[type='button'].next","button[aria-label='Next' i]"],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"application was submitted"},{"text":"Your application has been received"}],"multipage":true,"notes":"The job page is server-rendered; Apply (a.js-oneclick, #st-apply) opens the oneclick-ui application, a JS app the curl fetch could not read (403). Field selectors beyond the autocomplete/type attributes are therefore fallbacks that defer to the label matcher. The apply flow is multi-step (contact, resume, questions, review)."},{"id":"recruitee","name":"Recruitee","url_patterns":["https://*.recruitee.com/o/*","https://jobs.*/o/*/c/new","https://*/o/*/c/new"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML (server-rendered form#offer-application-form)","urls":["https://jobs.channable.com/o/talent-acquisition-partner/c/new"]},"form":"form#offer-application-form","fields":{"name":["input[name='candidate.name']","input[autocomplete='name']"],"email":["input[name='candidate.email']","input[autocomplete='email']"],"phone":["input[name='candidate.phone']","input[autocomplete='tel']"],"resume":["input[name='candidate.cv']","input[type='file'][data-cy='fileInputField']"]},"continue_buttons":["button[data-cy='apply-button']"],"submit_buttons":["button[data-testid='submit-application-form-button']","form#offer-application-form button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been sent"},{"text":"We have received your application"}],"multipage":false,"notes":"Hosted on the company's own careers domain (jobs.<company>.com/o/<slug>/c/new) as well as <company>.recruitee.com. Server-rendered: candidate.name, candidate.email, candidate.phone, candidate.cv (file), candidate.coverLetterFile, custom questions as candidate.openQuestionAnswers.<id>.content (text/radio), consent checkbox candidate.agreements.0.consent. Submit is 'Send' (data-testid=submit-application-form-button). The job page's Apply button (data-cy=apply-button) switches to the application tab, it does not submit."},{"id":"workday","name":"Workday","url_patterns":["https://*.myworkdayjobs.com/*","https://*.myworkdaysite.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell only: <div id='root'>); selectors are Workday's data-automation-id attributes, the stable hooks Workday ships on every candidate-home form","urls":["https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"]},"form":"form","fields":{"first_name":["[data-automation-id='legalNameSection_firstName']"],"last_name":["[data-automation-id='legalNameSection_lastName']"],"email":["[data-automation-id='email']"],"phone":["[data-automation-id='phone-number']"],"street":["[data-automation-id='addressSection_addressLine1']"],"city":["[data-automation-id='addressSection_city']"],"state":["[data-automation-id='addressSection_countryRegion']"],"zip":["[data-automation-id='addressSection_postalCode']"],"country":["[data-automation-id='countryDropdown']","[data-automation-id='addressSection_country']"],"linkedin":["[data-automation-id='linkedinQuestion'] input","input[data-automation-id*='linkedin' i]"],"resume":["[data-automation-id='file-upload-input-ref']","input[type='file']"]},"continue_buttons":["[data-automation-id='bottom-navigation-next-button']","button[data-automation-id='pageFooterNextButton']"],"submit_buttons":["[data-automation-id='bottom-navigation-next-button'][aria-label*='Submit' i]","button[data-automation-id='pageFooterNextButton'][aria-label*='Submit' i]"],"success_markers":[{"text":"Congratulations"},{"text":"You have successfully submitted"},{"text":"application has been submitted"},{"text":"Thank you for applying"}],"multipage":true,"notes":"Workday renders everything client-side behind a candidate account; the application is a multi-page wizard (My Information, My Experience, Application Questions, Voluntary Disclosures, Self Identify, Review). The last page's Next button reads 'Submit', so submit_buttons match the same navigation button only when its label says Submit. Phone device type and country code are data-automation-id='phone-device-type' / 'country-phone-code' dropdowns the person picks. Sign-in and account creation are the person's (an auth wall aborts the fill)."},{"id":"icims","name":"iCIMS","url_patterns":["https://*.icims.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of the job page and the apply/login step inside the iCIMS content iframe (in_iframe=1)","urls":["https://careers-lw.icims.com/jobs/6713/financial-programmer/job?in_iframe=1","https://fdcnmcareers-ulta.icims.com/jobs/login?in_iframe=1"]},"frame":"iframe#icims_content_iframe","form":"form","fields":{"email":["form#enterEmailForm #email","#email","input[type='email']"],"phone":["#phoneNumber","input[name='phoneNumber']"],"first_name":["#firstname","input[name='firstname']","input[autocomplete='given-name']"],"last_name":["#lastname","input[name='lastname']","input[autocomplete='family-name']"],"resume":["input[type='file']"]},"continue_buttons":["#enterEmailSubmitButton","input.iCIMS_PrimaryButton[type='submit'][value='Next']","a.iCIMS_ApplyOnlineButton"],"submit_buttons":["input[type='submit'][value*='Submit' i]","button[type='submit'][class*='Submit' i]"],"success_markers":[{"text":"Thank you for applying"},{"text":"application has been received"},{"text":"successfully submitted"}],"multipage":true,"notes":"The career portal lives inside iframe#icims_content_iframe on the employer's page; the fill engine runs in the frame's own document. Apply starts with an email step (form#enterEmailForm: #email, #countryCode, #phoneNumber, submit #enterEmailSubmitButton 'Next') followed by a login or account-creation wall that is the person's. The later profile pages were not reachable without an account, so their fields fall through to the label matcher."},{"id":"bamboohr","name":"BambooHR","url_patterns":["https://*.bamboohr.com/careers/*","https://*.bamboohr.com/jobs/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shell: <div id='poRoot'>, staticfe.bamboohr.com bundles; the application form chunk is lazy-loaded and not in the raw bundle)","urls":["https://fleetio.bamboohr.com/careers","https://staticfe.bamboohr.com/fe-main/prod/js/po.5d4a075d.dist.js"]},"form":"form","fields":{"first_name":["input[name='firstName']","#firstName","input[autocomplete='given-name']"],"last_name":["input[name='lastName']","#lastName","input[autocomplete='family-name']"],"email":["input[name='email']","#email","input[type='email']"],"phone":["input[name='phone']","#phone","input[type='tel']"],"resume":["input[type='file']"]},"continue_buttons":[],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"We have received your application"}],"multipage":false,"notes":"Careers pages are a React app (#poRoot) on <company>.bamboohr.com/careers/<id>; the application form itself is a lazy chunk the curl fetch could not read, so the field selectors are attribute fallbacks that defer to the label matcher."},{"id":"jobvite","name":"Jobvite","url_patterns":["https://jobs.jobvite.com/*","https://*.jobvite.com/*"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML of Jobvite's own careers site (jv-page markup, no open posting) and of customer slugs, which redirect to jobvite.com/support/job-seeker-support/?invalid=1 when the slug is not a live site; no apply form was reachable, so the fields are attribute fallbacks","urls":["https://jobs.jobvite.com/careers/jobvite","https://jobs.jobvite.com/jobvite/jobs"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='first' i]"],"last_name":["input[autocomplete='family-name']","input[name*='last' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["button.jv-button-primary[type='button']"],"submit_buttons":["button[type='submit']","input[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"received your application"}],"multipage":true,"notes":"Career sites are jobs.jobvite.com/<company>/job/<id> with the application at .../apply (jv-* markup). Only the site shell could be read live (no open posting), so the fields are attribute fallbacks that defer to the label matcher."},{"id":"taleo","name":"Oracle Taleo","url_patterns":["https://*.taleo.net/careersection/*","https://*.tbe.taleo.net/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"not readable: hilton.taleo.net/careersection/ answered 'Career Section Unavailable' and chp.tbe.taleo.net answered 'Come Back Soon' (HTTP 500); other *.taleo.net hosts did not resolve or redirected to the employer's new career site. The host families are real, no form was reachable, so no selector here is verified","urls":["https://hilton.taleo.net/careersection/hilton_external/jobsearch.ftl","https://chp.tbe.taleo.net/chp03/ats/careers/v2/searchResults?org=TEALIUM&cws=37"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='firstName' i]","input[id*='firstName' i]"],"last_name":["input[autocomplete='family-name']","input[name*='lastName' i]","input[id*='lastName' i]"],"email":["input[type='email']","input[name*='email' i]","input[id*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["input[type='submit'][value*='Continue' i]","button[type='submit'][title*='Continue' i]","a[id*='next' i]"],"submit_buttons":["input[type='submit'][value*='Submit' i]","button[type='submit'][title*='Submit' i]"],"success_markers":[{"text":"Thank you"},{"text":"submission has been received"},{"text":"application has been submitted"}],"multipage":true,"notes":"No live Taleo career section could be fetched for this version, so there are no verified selectors: the entry carries URL patterns, generic attribute fallbacks and the multi-step wizard hint, and every field defers to the label matcher. Taleo career sections sit behind a candidate sign-in (the person's)."},{"id":"successfactors","name":"SAP SuccessFactors","url_patterns":["https://*.successfactors.com/*","https://*.successfactors.eu/*","https://*/talentcommunity/apply/*","https://*/job/*/apply/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of a SuccessFactors career site (rmk markup): job page with a.dialogApplyBtn[href*='/talentcommunity/apply/']; the apply URL redirected to the site home (sign-in required)","urls":["https://jobs.adidas-group.com/job/Barcelona-Sales-Associate-Catalunya/1395009233/","https://jobs.adidas-group.com/talentcommunity/apply/1395009233/?locale=en_US"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='firstName' i]","input[id*='firstName' i]"],"last_name":["input[autocomplete='family-name']","input[name*='lastName' i]","input[id*='lastName' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["a.dialogApplyBtn","button[type='submit'][title*='Next' i]","input[type='submit'][value*='Next' i]"],"submit_buttons":["button[title*='Apply' i][type='submit']","input[type='submit'][value*='Apply' i]","button[type='submit'][title*='Submit' i]"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"successfully applied"}],"multipage":true,"notes":"Career sites run on the employer's own domain (jobs.<company>.com) with the SuccessFactors Recruiting Marketing markup; the application (talentcommunity/apply) sits behind the candidate profile sign-in, which the person completes. Beyond the apply link, no form fields were readable, so the fields defer to the label matcher."},{"id":"oracle_recruiting","name":"Oracle Cloud Recruiting","url_patterns":["https://*.oraclecloud.com/hcmUI/CandidateExperience/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (Knockout JS shell) + the live oj-hcm-ce bundle's apply-flow templates (section-personal-information, section-thank-you, apply-flow-pagination buttons)","urls":["https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/337010","https://static.oracle.com/cdn/fa/oj-hcm-ce/2607.26.262300322/js/main-minimal.js"]},"form":"form","fields":{"email":["input[type='email']","input[autocomplete='email']"],"phone":["input[type='tel']"],"first_name":["input[autocomplete='given-name']"],"last_name":["input[autocomplete='family-name']"],"resume":["input[type='file']"]},"continue_buttons":[".apply-flow-pagination__button:not(.apply-flow-pagination__submit-button)","button[class*='apply-flow-pagination__button']"],"submit_buttons":[".apply-flow-pagination__submit-button","button.submit-button[class*='apply-flow']"],"success_markers":[{"selector":".apply-flow-post-summary-thank-you"},{"selector":"[class*='section-thank-you']"},{"text":"Thank you for applying"},{"text":"application has been submitted"}],"multipage":true,"notes":"The apply flow starts with an email (then a verification code the person types), then the personal-information, experience, more-about-you and confirm sections, each a page of the wizard (apply-flow-pagination buttons). Field inputs are rendered from the employer's configuration with no stable ids, so they defer to the label matcher; the pagination and thank-you class names come from the live bundle's templates."},{"id":"eightfold","name":"Eightfold","url_patterns":["https://*.eightfold.ai/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (pcsx shell) + the live pcsxPwa bundle (#apply-form-container, data-test-id stepper hooks)","urls":["https://micron.eightfold.ai/careers","https://micron.eightfold.ai/gen/js/pcsxPwa.fa5d073d8912644a.js"]},"form":"#apply-form-container form","fields":{"first_name":["#apply-form-container input[autocomplete='given-name']","#apply-form-container input[name*='first' i]"],"last_name":["#apply-form-container input[autocomplete='family-name']","#apply-form-container input[name*='last' i]"],"email":["#apply-form-container input[type='email']","#apply-form-container input[name*='email' i]"],"phone":["#apply-form-container input[type='tel']","#apply-form-container input[name*='phone' i]"],"resume":["#apply-form-container input[type='file']","input[type='file']"]},"continue_buttons":["#apply-form-container button[type='button'][class*='next' i]","[data-test-id^='step-'] button[type='button']"],"submit_buttons":["#apply-form-container button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"application has been submitted"},{"text":"successfully submitted"}],"multipage":true,"notes":"Career sites are <company>.eightfold.ai/careers; the Smart Apply drawer renders inside #apply-form-container as a stepper (data-test-id='step-<n>'). Its inputs carry no stable ids in the shipped bundle, so the fields are attribute fallbacks scoped to the drawer that defer to the label matcher."},{"id":"phenom","name":"Phenom (careers.<company>.com)","url_patterns":["https://careers.*/*","https://jobs.*/*","https://*.phenompeople.com/*"],"detect_markers":["script[src*='phenompeople']","script[src*='phenom']","[data-ph-at-id]","#phApp"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML of two Phenom career sites (phenompeople / phenomtrack markers); Phenom hands the application to the employer's ATS","urls":["https://www.pepsicojobs.com/main/jobs","https://jobs.cvshealth.com/us/en/home"]},"handoff":true,"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='first' i]"],"last_name":["input[autocomplete='family-name']","input[name*='last' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["a[data-ph-at-id='apply-link']","a.btn[href*='apply' i]","button[data-ph-at-id*='apply' i]"],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you for applying"},{"text":"application has been submitted"}],"multipage":true,"notes":"Phenom is the career-site layer on the employer's own domain (careers.<company>.com, jobs.<company>.com); it is recognised by its DOM markers rather than the host. 'Apply' hands off to the employer's ATS (Workday, SuccessFactors, iCIMS...), where that system's recipe takes over, so this entry mainly shows the apply hand-off hint and defers any on-site form to the label matcher."},{"id":"avature","name":"Avature","url_patterns":["https://*.avature.net/*","https://*/careers/Register*","https://*/careers/Login*","https://*/careers/JobDetail/*","https://*/careers/ApplicationMethods*"],"detect_markers":["meta[name='avature.portal.id']","input.WizardFieldInput","form.tpt_wizard"],"js_rendered":false,"verified":{"date":"2026-10-11","method":"curl raw HTML (server-rendered wizard form.tpt_wizard on a custom-domain Avature portal)","urls":["https://jobs.lenovo.com/en_US/careers/JobDetail/82554","https://jobs.lenovo.com/en_US/careers/Register?jobId=82554"]},"form":"form.tpt_wizard","fields":{"first_name":["form.tpt_wizard input[autocomplete='given-name']"],"last_name":["form.tpt_wizard input[autocomplete='family-name']"],"phone":["form.tpt_wizard input[type='tel'][autocomplete='tel']"],"email":["form.tpt_wizard input.EmailField","form.tpt_wizard input[autocomplete='email']"],"street":["form.tpt_wizard input.StreetField","form.tpt_wizard input[autocomplete='street-address']"],"city":["form.tpt_wizard input.CityField","form.tpt_wizard input[autocomplete='address-level2']"],"state":["form.tpt_wizard select[autocomplete='address-level1']"],"zip":["form.tpt_wizard input.PostalCodeField","form.tpt_wizard input[autocomplete='postal-code']"],"country":["form.tpt_wizard select.countryFieldSelect"],"resume":["form.tpt_wizard input[type='file']"]},"continue_buttons":["button.nextButton","button[name='next']"],"submit_buttons":["button[name='submit']","button.WizardButtonPrimary[type='submit'][name='finish']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"successfully submitted"}],"multipage":true,"notes":"Avature portals run on the employer's domain (jobs.<company>.com/en_US/careers/...) and are recognised by their meta avature.portal.* tags and the WizardField markup. Inputs have numeric ids (e.g. #353) that differ per portal, so the stable hooks are the autocomplete attributes and the field classes (TextField, EmailField, PostalCodeField, StreetField, CityField, countryFieldSelect). The wizard's Continue is button.nextButton[name=next]."},{"id":"adp","name":"ADP (Workforce Now / myjobs)","url_patterns":["https://workforcenow.adp.com/mascsr/default/mdf/recruitment/*","https://myjobs.adp.com/*","https://*.adp.com/*"],"js_rendered":true,"verified":{"date":"2026-10-11","method":"curl raw HTML (JS shells only: #recruitment_root on Workforce Now, <cx-root> on myjobs.adp.com)","urls":["https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html","https://myjobs.adp.com/"]},"form":"form","fields":{"first_name":["input[autocomplete='given-name']","input[name*='first' i]","input[id*='first' i]"],"last_name":["input[autocomplete='family-name']","input[name*='last' i]","input[id*='last' i]"],"email":["input[type='email']","input[name*='email' i]"],"phone":["input[type='tel']","input[name*='phone' i]"],"resume":["input[type='file']"]},"continue_buttons":["button[type='button'][class*='next' i]","button[aria-label='Next' i]"],"submit_buttons":["button[type='submit']"],"success_markers":[{"text":"Thank you"},{"text":"application has been submitted"},{"text":"successfully submitted"}],"multipage":true,"notes":"Both ADP candidate experiences are JS applications whose forms were not readable by curl, so the fields are attribute fallbacks that defer to the label matcher. Workforce Now career centres need a cid parameter per employer."}]}/*RECIPES_JSON_END*/;
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
    for (const el of qsaSafe(sel)) {
      if (!el || !CONTROL_TAGS.has(String(el.tagName || "").toUpperCase())) continue;
      if (taken.has(el)) continue;
      const t = String(el.tagName).toUpperCase() === "INPUT" ? String(el.type || "text").toLowerCase() : "";
      if (t === "file" || t === "hidden" || t === "checkbox" || t === "radio" || t === "password") continue;
      if (!shown(el)) continue;
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
      for (const el of qsaSafe(sel)) if (el && shown(el)) return el;
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
    const body = norm((document.body && document.body.textContent) || "").slice(0, 20000);
    const markers = (r && r.success_markers) || [];
    // A "thank you" PHRASE only counts once the form is gone: while the recipe's submit button
    // (or, with no recipe, a file upload) is still on the page, the person has not submitted,
    // whatever the page says elsewhere. A selector or URL marker is specific enough on its own.
    const formOpen = r ? !!firstShown(r.submit_buttons) : qsaSafe("input[type='file']").some(shown);
    for (const m of markers) {
      if (m.selector && qsaSafe(m.selector).length) return true;
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
  }
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
      const n = document.getElementById(id); if (n) bits.push(n.textContent);
    });
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
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
    if ((rect.right !== undefined && rect.right <= 0) ||
        (rect.bottom !== undefined && rect.bottom <= 0)) return false;    // parked off-canvas
    const gcs = (typeof window.getComputedStyle === "function") ? window.getComputedStyle(el) : null;
    if (gcs && (gcs.display === "none" || gcs.visibility === "hidden" ||
                gcs.opacity === "0")) return false;
    return true;
  }

  function candidates() {
    const out = [];
    document.querySelectorAll("input, textarea, select").forEach((el) => {
      if (!shown(el)) return;
      const tag = el.tagName.toLowerCase();
      if (tag === "input") {
        const t = (el.type || "text").toLowerCase();
        if (!FILLABLE_TYPES.has(t)) return;                // skip file/checkbox/radio/password/submit
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
      taken.add(el);
      plan.push({ el, key, value, via: "label" });
    }
    return plan;
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
  const DECLINE_RE = /decline|prefer not|not to (answer|identify|disclose)|don'?t (wish|want)|do not (wish|want)|i do not/;
  const YES_RE = /^\W*(yes|y|i am\b|i do\b|true)/;
  const NO_RE  = /^\W*(no|n|i am not|i do not|i'?m not|false)/;

  function radioOptionText(r) {
    if (r.id) { const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`); if (l) return norm(l.textContent); }
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
  function groupQuestion(control) {
    const fs = control.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return norm(lg.textContent); }
    const radioName = (String(control.type || "").toLowerCase() === "radio") ? control.name : null;
    let node = control.parentElement;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      const tag = String(node.tagName || "").toUpperCase();
      if (tag === "FORM" || tag === "BODY" || tag === "HTML") break;
      const others = qsaSafe("input, select, textarea", node).filter((o) =>
        o !== control && String(o.type || "").toLowerCase() !== "hidden" &&
        !(radioName && String(o.type || "").toLowerCase() === "radio" && o.name === radioName));
      if (others.length) break;                       // past this question's box
      for (const lab of qsaSafe(QUESTION_LABEL, node)) {
        // An option's label wraps its control (this one's or a sibling's); the question's never does.
        if (lab.contains(control) || qsaSafe("input, select, textarea", lab).length) continue;
        const t = norm(lab.textContent);
        if (t) return t;
      }
      const aria = node.getAttribute && node.getAttribute("aria-label"); if (aria) return norm(aria);
    }
    return norm(control.getAttribute("aria-label") || "");
  }

  function radioGroups() {
    const groups = new Map();
    let n = 0;
    document.querySelectorAll('input[type="radio"]').forEach((r) => {
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
    document.querySelectorAll("select").forEach((sel) => routeAndFill(sel, null));
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
    document.querySelectorAll("select").forEach((sel) => {
      const q = groupQuestion(sel) || signature(sel);
      if (topics.test(q) && pickSelectOption(sel, (t) => decline.test(t))) count++;
    });
    return count;
  }

  // A résumé/CV file input we can SEE but cannot fill (browsers block setting file inputs
  // from script). Surface it so the person attaches their tailored PDF themselves.
  function hasResumeUpload() {
    for (const f of document.querySelectorAll('input[type="file"]')) {
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
  function rawLabel(el) {
    const fs = el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return lg.textContent.trim(); }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.textContent.trim();
    }
    const p = el.closest("label"); if (p) return p.textContent.trim();
    const grp = el.closest("div,li,section,fieldset");
    if (grp) { const l = grp.querySelector("label, legend, .label, [class*='question']"); if (l) return l.textContent.trim(); }
    return (el.getAttribute("aria-label") || el.placeholder || "").trim();
  }

  function answerableTextareas() {
    const out = [];
    document.querySelectorAll("textarea").forEach((t) => {
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
  function pageBlocker() {
    for (const f of document.querySelectorAll("iframe")) {
      const src = norm(f.getAttribute && f.getAttribute("src"));
      if (/recaptcha|hcaptcha|\bcaptcha\b|turnstile|arkoselabs|funcaptcha|challenges\.cloudflare/.test(src))
        return "captcha";
    }
    const body = norm((document.body && document.body.textContent) || "").slice(0, 5000);
    if (/are you (a )?human|verify (that )?you.?re (a )?human|verify you are human|i'?m not a robot|complete the captcha|press (and|&) hold|unusual traffic|checking your browser before/.test(body))
      return "captcha";
    const pw = document.querySelector('input[type="password"]');
    if (pw && (!pw.getBoundingClientRect || pw.getBoundingClientRect().width > 0)) return "auth";
    if (/(please )?(sign in|log ?in) to (apply|continue|view this)|you must (sign in|log ?in|be logged in)|create an account to apply|sign in to your account to continue/.test(body))
      return "auth";
    return "";
  }

  // One-call orchestrator for the desktop shell: fill everything the payload supports and report
  // "filled X of Y" so the person knows exactly what to review. Never clicks submit.
  function fill(payload) {
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
      r = "t" + (_refN++);
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
    return ["email", "tel", "url", "date", "number"].includes(t) ? t : "text";
  }

  // The interactable fields on the form, STRUCTURE ONLY (labels/types/options, never any value),
  // each with a stable ref the executor targets. This is exactly what goes to the brain.
  // With {onlyEmpty: true} it lists just what is still blank after the recipe + label passes,
  // which is all the model-plan fallback should be asked about.
  function extractForm(opts) {
    const onlyEmpty = !!(opts && opts.onlyEmpty);
    const fields = [];
    document.querySelectorAll("input, textarea, select").forEach((el) => {
      if (el.disabled || el.readOnly) return;
      const raw = el.tagName.toLowerCase() === "input" ? (el.type || "text").toLowerCase() : el.tagName.toLowerCase();
      if (["radio", "hidden", "submit", "button", "reset", "password", "image"].includes(raw)) return;
      if (onlyEmpty && raw !== "file" && raw !== "checkbox" && String(el.value || "").trim()
          && !/^(select|choose|--|please)/i.test(String(el.value))) return;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0 && raw !== "file") return;   // truly hidden (files can be styled away)
      const label = rawLabel(el);
      if (!label && raw !== "file") return;                                  // need a question to answer
      const f = { ref: fieldRef(el), label: (label || "Résumé / CV").slice(0, 200),
                  type: fieldType(el), required: !!(el.required || /\*\s*$/.test(label || "")) };
      if (el.tagName.toLowerCase() === "select") {
        f.options = [...el.options].map((o) => (o.textContent || o.value).trim())
          .filter((t) => t && !/^(select|choose|--|please)/i.test(t)).slice(0, 40);
      }
      fields.push(f);
    });
    for (const radios of radioGroups()) {                                    // one field per radio group
      const q = groupQuestion(radios[0]);
      if (!q) continue;
      if (onlyEmpty && radios.some((r) => r.checked)) continue;
      const ref = fieldRef(radios[0]);
      radios.forEach((r) => r.setAttribute("data-tailor-group", ref));
      fields.push({ ref, label: (rawLabel(radios[0]) || q).slice(0, 200), type: "radio",
                    options: radios.map((r) => radioOptionText(r)).filter(Boolean).slice(0, 40), required: false });
    }
    return fields;
  }

  const elByRef = (ref) => document.querySelector(`[data-tailor-ref="${CSS.escape(ref)}"]`);

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
      const type = fieldType(el);
      const text = String(op.text == null ? "" : op.text);
      try {
        if (type === "radio") {
          const radios = [...document.querySelectorAll(`[data-tailor-group="${CSS.escape(op.ref)}"]`)];
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

  window.TailorAutofill = {
    fill, pageBlocker, planFill, applyFill, fillScreening, fillTextAnswers, answerKeyForLabel,
    fillEeo, hasResumeUpload, candidates, categorize, signature, setNativeValue, markFilled,
    rawLabel, answerableTextareas, pageJobContext, screeningAnswer,
    extractForm, applyOps, fieldRef,                                         // AI-planned autoapply
    // recipes: the per-system table, success detection, continue hints, corrections
    setRecipes, recipes: () => RECIPES, recipeFor, globToRe, highlightSubmit, continueButton,
    detectSuccess, watchSuccess, stopSuccessWatch, watchCorrections, takeCorrections,
  };
})();
