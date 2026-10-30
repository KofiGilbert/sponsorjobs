"""Countries, for "Country of citizenship": the list the person picks from on Your answers, and
the match from their saved country to the words a form's own list uses ("United States" is
Workday's "United States of America", "South Korea" its "Korea, Republic of").

Pure data and string matching. A saved country that matches no option, or more than one, gives
None: the field stays open and the agent asks, never a guess.
"""
from __future__ import annotations

import re
import unicodedata

# Common English short names, sorted. Sovereign states plus the places a citizenship list
# usually carries on its own (Hong Kong, Macau, Taiwan, Palestine, Kosovo, Vatican City).
NAMES: tuple[str, ...] = (
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda", "Argentina",
    "Armenia", "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados",
    "Belarus", "Belgium", "Belize", "Benin", "Bhutan", "Bolivia", "Bosnia and Herzegovina", "Botswana",
    "Brazil", "Brunei", "Bulgaria", "Burkina Faso", "Burundi", "Cambodia", "Cameroon", "Canada",
    "Cape Verde", "Central African Republic", "Chad", "Chile", "China", "Colombia", "Comoros",
    "Costa Rica", "Côte d'Ivoire", "Croatia", "Cuba", "Cyprus", "Czech Republic",
    "Democratic Republic of the Congo", "Denmark", "Djibouti", "Dominica", "Dominican Republic",
    "Ecuador", "Egypt", "El Salvador", "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini", "Ethiopia",
    "Fiji", "Finland", "France", "Gabon", "Gambia", "Georgia", "Germany", "Ghana", "Greece", "Grenada",
    "Guatemala", "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hong Kong", "Hungary",
    "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy", "Jamaica", "Japan",
    "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos", "Latvia",
    "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania", "Luxembourg", "Macau",
    "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali", "Malta", "Marshall Islands", "Mauritania",
    "Mauritius", "Mexico", "Micronesia", "Moldova", "Monaco", "Mongolia", "Montenegro", "Morocco",
    "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal", "Netherlands", "New Zealand", "Nicaragua",
    "Niger", "Nigeria", "North Korea", "North Macedonia", "Norway", "Oman", "Pakistan", "Palau",
    "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines", "Poland", "Portugal",
    "Qatar", "Republic of the Congo", "Romania", "Russia", "Rwanda", "Saint Kitts and Nevis",
    "Saint Lucia", "Saint Vincent and the Grenadines", "Samoa", "San Marino", "Sao Tome and Principe",
    "Saudi Arabia", "Senegal", "Serbia", "Seychelles", "Sierra Leone", "Singapore", "Slovakia",
    "Slovenia", "Solomon Islands", "Somalia", "South Africa", "South Korea", "South Sudan", "Spain",
    "Sri Lanka", "Sudan", "Suriname", "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan",
    "Tanzania", "Thailand", "Timor-Leste", "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Turkey",
    "Turkmenistan", "Tuvalu", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
    "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Vatican City", "Venezuela", "Vietnam",
    "Yemen", "Zambia", "Zimbabwe",
)

# Other names forms use for the same country (official names, ISO's "Name, Republic of" order,
# older names). Only names that cannot mean another country.
ALIASES: dict[str, tuple[str, ...]] = {
    "United States": ("United States of America", "USA", "US", "U.S.", "U.S.A.", "America"),
    "United Kingdom": ("UK", "U.K.", "Great Britain", "Britain",
                       "United Kingdom of Great Britain and Northern Ireland"),
    "South Korea": ("Korea, Republic of", "Republic of Korea", "Korea (South)", "Korea, South"),
    "North Korea": ("Korea, Democratic People's Republic of", "Democratic People's Republic of Korea",
                    "Korea (North)", "Korea, North", "DPRK"),
    "Vietnam": ("Viet Nam",),
    "Russia": ("Russian Federation",),
    "Iran": ("Iran, Islamic Republic of", "Islamic Republic of Iran"),
    "Syria": ("Syrian Arab Republic",),
    "Laos": ("Lao People's Democratic Republic", "Lao PDR"),
    "Moldova": ("Moldova, Republic of", "Republic of Moldova"),
    "Tanzania": ("Tanzania, United Republic of", "United Republic of Tanzania"),
    "Venezuela": ("Venezuela, Bolivarian Republic of",),
    "Bolivia": ("Bolivia, Plurinational State of",),
    "Taiwan": ("Taiwan, Province of China", "Taiwan, Republic of China", "Republic of China"),
    "Turkey": ("Türkiye", "Turkiye"),
    "Czech Republic": ("Czechia",),
    "Côte d'Ivoire": ("Ivory Coast",),
    "North Macedonia": ("Macedonia", "Republic of North Macedonia",
                        "Macedonia, the former Yugoslav Republic of"),
    "Myanmar": ("Burma",),
    "Eswatini": ("Swaziland",),
    "Cape Verde": ("Cabo Verde",),
    "Brunei": ("Brunei Darussalam",),
    "Micronesia": ("Micronesia, Federated States of", "Federated States of Micronesia"),
    "Palestine": ("Palestine, State of", "State of Palestine", "Palestinian Territory"),
    "Hong Kong": ("Hong Kong SAR", "Hong Kong SAR, China", "Hong Kong, China"),
    "Macau": ("Macao", "Macao SAR", "Macau SAR"),
    "Bahamas": ("The Bahamas", "Bahamas, The"),
    "Gambia": ("The Gambia", "Gambia, The"),
    "Netherlands": ("The Netherlands", "Netherlands, The", "Holland"),
    "Democratic Republic of the Congo": ("Congo, Democratic Republic of the", "Congo (Kinshasa)", "DR Congo",
                                         "DRC", "Congo, The Democratic Republic of the"),
    "Republic of the Congo": ("Congo, Republic of the", "Congo (Brazzaville)"),
    "Timor-Leste": ("East Timor",),
    "Vatican City": ("Holy See", "Holy See (Vatican City State)"),
    "Sao Tome and Principe": ("São Tomé and Príncipe",),
    "Saint Kitts and Nevis": ("St. Kitts and Nevis", "St Kitts and Nevis"),
    "Saint Lucia": ("St. Lucia", "St Lucia"),
    "Saint Vincent and the Grenadines": ("St. Vincent and the Grenadines", "St Vincent and the Grenadines"),
    "Kyrgyzstan": ("Kyrgyz Republic",),
    "Slovakia": ("Slovak Republic",),
    "United Arab Emirates": ("UAE",),
}


def _key(s: str, *, parens: bool = True) -> str:
    """A name compared without case, accents, punctuation or "the"."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    if not parens:
        s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\bthe\b", " ", s)
    return " ".join(s.split())


_BY_KEY: dict[str, str] = {}
for _name in NAMES:
    _BY_KEY[_key(_name)] = _name
for _name, _alts in ALIASES.items():
    for _alt in _alts:
        _BY_KEY.setdefault(_key(_alt), _name)


def canonical(name: str) -> str:
    """The list's own name for a country name or alias ("USA" -> "United States"), "" when it is
    not a country this list knows."""
    k = _key(name)
    return _BY_KEY.get(k) or _BY_KEY.get(_key(name, parens=False)) or ""


def country_option(answer: str, options) -> str | None:
    """The option of a form's country list that names the saved country, or None when none or
    several do. "United States" picks "United States of America" and never "United States Minor
    Outlying Islands"."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    want = canonical(answer)
    if not opts or not want:
        return None
    for parens in (True, False):
        hits = [o for o in opts if _BY_KEY.get(_key(o, parens=parens)) == want]
        if len({_key(h) for h in hits}) == 1:
            return hits[0]
        if hits:
            return None
    return None
