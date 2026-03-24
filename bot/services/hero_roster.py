from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HeroRosterEntry:
    name: str
    aliases: tuple[str, ...]


# Source: Liquipedia Deadlock Portal:Heroes (main playable roster; Hero Labs excluded)
# https://liquipedia.net/deadlock/Portal:Heroes
HERO_ROSTER: tuple[HeroRosterEntry, ...] = (
    HeroRosterEntry("Abrams", ("abrams", "abram")),
    HeroRosterEntry("Apollo", ("apollo",)),
    HeroRosterEntry("Bebop", ("bebop", "bb")),
    HeroRosterEntry("Billy", ("billy",)),
    HeroRosterEntry("Calico", ("calico", "cali")),
    HeroRosterEntry("Celeste", ("celeste",)),
    HeroRosterEntry("Doorman", ("doorman",)),
    HeroRosterEntry("Drifter", ("drifter",)),
    HeroRosterEntry("Dynamo", ("dynamo",)),
    HeroRosterEntry("Graves", ("graves",)),
    HeroRosterEntry("Grey Talon", ("greytalon", "graytalon", "grey", "talon")),
    HeroRosterEntry("Haze", ("haze",)),
    HeroRosterEntry("Holliday", ("holliday", "holly", "holiday", "holi", "holli")),
    HeroRosterEntry("Infernus", ("infernus", "inferno")),
    HeroRosterEntry("Ivy", ("ivy",)),
    HeroRosterEntry("Kelvin", ("kelvin",)),
    HeroRosterEntry("Lady Geist", ("ladygeist", "geist", "lady")),
    HeroRosterEntry("Lash", ("lash",)),
    HeroRosterEntry("McGinnis", ("mcginnis", "mcg", "macginnis")),
    HeroRosterEntry("Mina", ("mina",)),
    HeroRosterEntry("Mirage", ("mirage",)),
    HeroRosterEntry("Mo and Krill", ("moandkrill", "mo&krill", "mokrill", "mo", "krill")),
    HeroRosterEntry("Paige", ("paige",)),
    HeroRosterEntry("Paradox", ("paradox",)),
    HeroRosterEntry("Pocket", ("pocket",)),
    HeroRosterEntry("Rem", ("rem",)),
    HeroRosterEntry("Seven", ("seven", "7")),
    HeroRosterEntry("Shiv", ("shiv",)),
    HeroRosterEntry("Silver", ("silver",)),
    HeroRosterEntry("Sinclair", ("themagnificentsinclair", "sinclair", "magnificentsinclair")),
    HeroRosterEntry("Venator", ("venator", "ven")),
    HeroRosterEntry("Victor", ("victor", "vic")),
    HeroRosterEntry("Vindicta", ("vindicta", "vindi")),
    HeroRosterEntry("Viscous", ("viscous", "goo")),
    HeroRosterEntry("Vyper", ("vyper", "viper")),
    HeroRosterEntry("Warden", ("warden", "ward")),
    HeroRosterEntry("Wraith", ("wraith", "wrath")),
    HeroRosterEntry("Yamato", ("yamato", "yamo", "yam")),
)


def _normalize_alias(alias: str) -> str:
    return "".join(character for character in alias.casefold() if character.isalnum())


_HERO_BY_ALIAS: dict[str, str] = {}
for hero_entry in HERO_ROSTER:
    canonical_alias = _normalize_alias(hero_entry.name)
    _HERO_BY_ALIAS[canonical_alias] = hero_entry.name
    for alias in hero_entry.aliases:
        _HERO_BY_ALIAS[_normalize_alias(alias)] = hero_entry.name


def resolve_hero_alias(raw_alias: str) -> str | None:
    normalized = _normalize_alias(raw_alias)
    if not normalized:
        return None
    return _HERO_BY_ALIAS.get(normalized)


def list_playable_heroes() -> tuple[str, ...]:
    return tuple(hero_entry.name for hero_entry in HERO_ROSTER)

