

def test_workday_previously_employed_wording_is_the_canonical_question():
    from autoapply.answers import normalize
    wd = ("Were you previously an employee, contractor or intern of COUNTRY Financial, Illinois Farm Bureau, "
          "MiddleOak, Cotton States, Mutual Services Inc. (MSI), GROWMARK, an Illinois County Farm Bureau, "
          "or a GROWMARK FS Member Company?")
    assert normalize(wd) == "@previously_worked"
    assert normalize("Have you ever worked for us before?") == "@previously_worked"
    assert normalize("Were you previously an employee of this company or its affiliates?") == "@previously_worked"
