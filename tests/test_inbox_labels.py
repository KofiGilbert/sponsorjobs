"""The Inbox labels (inbox/labels.py): rules only, on made-up mail in the style the common
applicant tracking systems send. The same cases, in short, are in worker/test/mailbox.test.ts,
because the Worker's notice subject is a port of these rules."""
from __future__ import annotations

import pytest

from inbox.labels import LABELS, STAGE_FOR_LABEL, label_of

# ---- confirmations --------------------------------------------------------------------------

GREENHOUSE_RECEIVED = ("Thank you for applying to Acme Robotics", """Hi Zoe,

Thank you for applying to the Data Engineer role at Acme Robotics. We have received your
application and our team will review it shortly.

If your experience looks like a match, we will reach out to schedule an interview. If selected,
we will contact you by e-mail.

Best,
The Acme Robotics Recruiting Team""")

WORKDAY_RECEIVED = ("Borealis Labs: Application Received", """Dear Zoe Muller,

Your application for R-10442 Analyst, Supply Chain has been received.

You can check the status of your application at any time by signing in to your candidate home.
Should your qualifications match our needs, a recruiter will contact you about next steps.

This is an automated message. Replies to this address are not monitored.""")

LEVER_RECEIVED = ("Thanks for applying to Corvid", """Hi Zoe,

Thanks for applying to Corvid! We've received your application for Product Analyst.

Unfortunately, due to the number of applications we cannot respond to every applicant
individually. Our process typically includes a phone screen and a technical interview.

The Corvid team""")

ASHBY_RECEIVED = ("Application received: Platform Engineer at Dunmore", """Hello Zoe,

We have your application for Platform Engineer and it was successfully submitted. We are
reviewing applications on a rolling basis and will be in touch whether or not we move forward.

Dunmore Talent""")

TEAMTAILOR_RECEIVED = ("We got it! Your application to Elkhorn Studio", """Hi Zoe,

Thank you for your application to the position of Junior Designer at Elkhorn Studio.
We will go through all applications and get back to you as soon as we can. If you are not
selected for this role, we would still love to keep in touch about future openings.

Kind regards,
Elkhorn Studio""")

ADP_RECEIVED = ("Application Confirmation", """Thank you for your interest in Fernhill Foods.

Your application has been submitted for Requisition 884213, Financial Analyst I.
Only candidates selected for an interview will be contacted.

Please do not reply to this message.""")

# ---- rejections -----------------------------------------------------------------------------

GREENHOUSE_REJECTED = ("Your application to Acme Robotics", """Hi Zoe,

Thank you for your interest in the Data Engineer position at Acme Robotics. After careful
consideration, we have decided to pursue other candidates whose experience more closely
matches our needs at this time.

We appreciate the time you invested and wish you the best in your search.

Acme Robotics Recruiting""")

WORKDAY_REJECTED = ("Update on your application for R-10442 Analyst, Supply Chain", """Dear Zoe,

Thank you for applying. We regret to inform you that you have not been selected for this
position. We encourage you to apply for other roles that fit your skills.

Borealis Labs Talent Acquisition""")

LEVER_REJECTED = ("Corvid: an update", """Hi Zoe,

Thanks again for taking the time to interview with us. Unfortunately, we will not be moving
forward with your application for the Product Analyst role.

The Corvid team""")

ASHBY_REJECTED = ("Regarding your application", """Hello Zoe,

We wanted to let you know that the Platform Engineer position has been filled. We will keep
your details on file.

Dunmore Talent""")

TEAMTAILOR_REJECTED = ("Thank you for your application", """Hi Zoe,

Unfortunately we have chosen to go ahead with another candidate this time, so we are not able
to offer you the position.

Elkhorn Studio""")

ADP_REJECTED = ("Status of your application", """Fernhill Foods has reviewed your application for
Financial Analyst I. At this time we are not moving forward with your candidacy.""")

# ---- interviews -----------------------------------------------------------------------------

INTERVIEW_INVITE = ("Interview with Acme Robotics", """Hi Zoe,

We enjoyed reading your application and would like to invite you to a 30 minute phone
interview with our hiring manager. Please pick a time that works for you:
https://calendly.com/acme-robotics/phone-screen

Thanks,
Priya""")

INTERVIEW_AVAILABILITY = ("Next steps for Product Analyst", """Hello Zoe,

Thanks for your patience. We would love to speak with you about the role. Could you share your
availability for a video call next week?

Sam, Corvid""")

INTERVIEW_SCHEDULER = ("Schedule your interview with Borealis Labs", """Dear Zoe,

Congratulations, you have moved to the next round. Use the link below to schedule your
interview: https://borealis.example/scheduling/abc123""")

# ---- action ---------------------------------------------------------------------------------

ASSESSMENT = ("Complete your assessment for Dunmore", """Hello Zoe,

As the next step, please complete the online assessment on HackerRank within five days:
https://www.hackerrank.com/test/abc/login""")

VERIFY = ("Verify your email address", """Welcome to the Fernhill Foods careers site.
Please verify your email to finish creating your account:
https://careers.fernhill.example/verify?token=abc""")

CODE = ("Your verification code", "Your one-time code is 482913. It expires in 10 minutes.")

DOCUMENTS = ("Action required: additional information needed", """Dear Zoe,

To continue with your application we need a copy of your transcript. Please upload it to your
candidate profile by Friday.""")

FORM = ("One more step", """Hi Zoe, please complete the work authorization form so we can
continue reviewing your application: https://elkhorn.example/forms/wa""")

# ---- other ----------------------------------------------------------------------------------

NEWSLETTER = ("New jobs you may like", "Here are five new roles posted this week near Boston.")
PERSONAL = ("Lunch on Friday?", "Are you around? How did the interview go?")


@pytest.mark.parametrize("msg", [GREENHOUSE_RECEIVED, WORKDAY_RECEIVED, LEVER_RECEIVED, ASHBY_RECEIVED,
                                 TEAMTAILOR_RECEIVED, ADP_RECEIVED])
def test_confirmations_are_received(msg):
    assert label_of(*msg) == "received"


@pytest.mark.parametrize("msg", [GREENHOUSE_REJECTED, WORKDAY_REJECTED, LEVER_REJECTED, ASHBY_REJECTED,
                                 TEAMTAILOR_REJECTED, ADP_REJECTED])
def test_rejections_are_rejected(msg):
    assert label_of(*msg) == "rejected"


@pytest.mark.parametrize("phrase", [
    "You have not been selected for this role.",
    "We are not moving forward with your application.",
    "We have decided to pursue other candidates.",
    "Unfortunately, we are unable to offer you an interview.",
    "We regret to inform you that we have chosen another applicant.",
    "The position has been filled.",
    "We will not be proceeding with your application.",
    "You are no longer under consideration for this role.",
])
def test_each_rejection_wording_is_covered(phrase):
    assert label_of("Your application", f"Dear Zoe,\n\nThank you for applying. {phrase}\n\nRegards") == "rejected"


@pytest.mark.parametrize("msg", [INTERVIEW_INVITE, INTERVIEW_AVAILABILITY, INTERVIEW_SCHEDULER])
def test_interview_requests_are_interview(msg):
    assert label_of(*msg) == "interview"


@pytest.mark.parametrize("msg", [ASSESSMENT, VERIFY, CODE, DOCUMENTS, FORM])
def test_requests_for_something_are_action(msg):
    assert label_of(*msg) == "action"


@pytest.mark.parametrize("msg", [NEWSLETTER, PERSONAL, ("", "")])
def test_everything_else_is_other(msg):
    assert label_of(*msg) == "other"


def test_a_confirmation_that_says_if_selected_is_neither_interview_nor_rejected():
    body = ("Thank you for applying. We have received your application.\n"
            "If selected, we will reach out to schedule an interview. "
            "If you are not selected, we will let you know.")
    assert label_of("Application received", body) == "received"
    assert label_of("Application received", "We received your application. Should we decide not to "
                    "move forward, you will hear from us.") == "received"


def test_small_print_with_unfortunately_does_not_reject():
    assert label_of("Thanks for applying", "We received your application. Unfortunately we are not able "
                    "to give feedback to every applicant.") == "received"


def test_a_confirmation_number_is_not_a_one_time_code():
    assert label_of("Application Confirmation", "Thank you for applying. Confirmation number 884213.") == "received"


def test_a_rejection_after_an_interview_wins_over_the_word_interview():
    assert label_of(*LEVER_REJECTED) == "rejected"
    assert label_of("Update", "Thank you for the phone interview last week. Unfortunately we have decided "
                    "to move forward with other candidates.") == "rejected"


def test_every_label_has_a_stage_and_inputs_can_be_none():
    assert set(STAGE_FOR_LABEL) == set(LABELS)
    assert label_of(None, None) == "other"


def test_a_sentence_wrapped_across_lines_is_read_as_one():
    assert label_of("Update", "We are sorry to say that you have not\nbeen selected for this role.") == "rejected"
    assert label_of("Application received", "We received your application. If you are not\nselected, "
                    "we will let you know.") == "received"
