from ade_radar.normalize import NamePattern, clean_artist, night_of, norm, pretty_when, split_lineup, text_tokens


def test_clean_artist_strips_country_and_suffixes():
    assert clean_artist("Amelie Lens (BE)") == "Amelie Lens"
    assert clean_artist("Josh Baker (GB) all night long") == "Josh Baker"
    assert clean_artist("Prospa live") == "Prospa"
    assert clean_artist("Kettama (IE) extended") == "Kettama"


def test_split_lineup():
    assert split_lineup("Paige Tomlinson b2b Bella Claxton") == ["Paige Tomlinson", "Bella Claxton"]
    assert split_lineup("Kyle Starkey Invites x Overbruggen") == ["Kyle Starkey Invites", "Overbruggen"]
    assert split_lineup("CamelPhat pres. EXPERIENCE") == ["CamelPhat", "EXPERIENCE"]
    assert split_lineup("Marsolo (GB), Alexandria (GB) & Marie Montexier (GB)") == [
        "Marsolo", "Alexandria", "Marie Montexier"]
    assert split_lineup("X CLUB. vs SOSA") == ["X CLUB.", "SOSA"]


def test_accents_and_case():
    assert norm("Obskür") == "obskur"
    p = NamePattern("Obskür")
    t = "OBSKUR all night long"
    assert p.search(t, norm(t), text_tokens(t))


def test_short_names_exact_only():
    p = NamePattern("MK")
    for text, ok in [("MK at Gashouder", True), ("mkultra night", False), ("Area10 w/ MK!", True),
                     ("MKTO", False)]:
        assert p.search(text, norm(text), text_tokens(text)) is ok, text


def test_handles_and_aliases():
    p = NamePattern("Paige Tomlinson", ["paigetomlinson"])
    t = "ADE pop-up w/ @paigetomlinson"
    assert p.search(t, norm(t), text_tokens(t))
    p2 = NamePattern("Josh Baker")
    t2 = "see you there @joshbakermusic"
    assert p2.search(t2, norm(t2), text_tokens(t2))
    t3 = "joshua bakery"
    assert not p2.search(t3, norm(t3), text_tokens(t3))


def test_night_of_after_midnight():
    assert str(night_of("2026-10-24", "02:00")) == "2026-10-23"
    assert str(night_of("2026-10-24", "23:00")) == "2026-10-24"
    assert pretty_when("2026-10-24", "02:00") == "Sat 24 Oct 02:00 (Fri night)"
    assert pretty_when(None, None) == "TBA"
