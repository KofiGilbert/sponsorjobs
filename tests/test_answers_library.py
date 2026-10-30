

def test_workday_previously_employed_wording_is_the_canonical_question():
    from autoapply.answers import normalize
    wd = ("Were you previously an employee, contractor or intern of COUNTRY Financial, Illinois Farm Bureau, "
          "MiddleOak, Cotton States, Mutual Services Inc. (MSI), GROWMARK, an Illinois County Farm Bureau, "
          "or a GROWMARK FS Member Company?")
    assert normalize(wd) == "@previously_worked"
    assert normalize("Have you ever worked for us before?") == "@previously_worked"
    assert normalize("Were you previously an employee of this company or its affiliates?") == "@previously_worked"


def test_annual_salary_wordings_are_the_salary_question():
    """Workday asks "What is your annual salary requirement?": the word "annual" must not hide
    the saved salary answer, or the agent stops to ask a question the person already answered."""
    from autoapply.answers import normalize
    for q in ("What is your annual salary requirement?",
              "Annual salary expectations",
              "What are your annual compensation expectations?",
              "Annual base salary requirement",
              "What is your expected annual salary?"):
        assert normalize(q) == "@salary", q
    # The person's current pay is a different question and never gets the desired salary.
    assert normalize("What is your current salary?") != "@salary"
    assert normalize("Current annual salary") != "@salary"


def test_saved_salary_answers_the_annual_wording(tmp_path):
    from autoapply.answers import AnswerLibrary
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        lib.save("What are your salary expectations?", "110000")
        hit = lib.lookup("What is your annual salary requirement?")
        assert hit and hit["answer"] == "110000"
    finally:
        lib.close()


def test_answer_saved_under_an_old_key_is_found_after_the_rules_widen(tmp_path):
    import sqlite3
    from autoapply.answers import AnswerLibrary
    db = tmp_path / "a.db"
    AnswerLibrary(db).close()
    con = sqlite3.connect(db)
    con.execute("INSERT INTO answers (key, question, answer, source) VALUES (?,?,?,?)",
                ("annual requirement salary", "What is your annual salary requirement?", "110000", "you"))
    con.execute("DELETE FROM meta WHERE k='keys_version'")
    con.commit(); con.close()
    lib = AnswerLibrary(db)
    try:
        hit = lib.lookup("What are your salary expectations?")
        assert hit and hit["answer"] == "110000"
    finally:
        lib.close()
