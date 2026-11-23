"""Interactive, dependency-free setup for a first US-based search."""
import json
import os
import re
import sys

from job_search_pipeline.env import load_env_files

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Postal codes, so "Round Rock, TX" typed into the comma-separated nearby list does not add "TX" as a local city
US_STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH "
    "OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())


def _phrase(text):
    """A lower-case regex for `text` as a whole phrase: not inside a longer word, even when it starts or ends
    with punctuation (".NET Developer", "C++ Developer")."""
    return r"(?<!\w)" + re.escape(text.lower()) + r"(?!\w)"


def _strong_phrase(text):
    """The phrase for strong_target_patterns, which the pipeline wraps in \\b...\\b: a \\b cannot sit beside an
    edge character such as the "." in ".NET", so a multi-word title drops its edge punctuation."""
    trimmed = re.sub(r"^\W+|\W+$", "", text)
    return re.escape((trimmed if " " in trimmed else text).lower())


def _list(prompt, minimum=1):
    while True:
        values = []
        seen = set()
        for item in input(prompt).split(","):
            item = item.strip()
            if item and item.casefold() not in seen:
                values.append(item)
                seen.add(item.casefold())
        if len(values) >= minimum:
            return values
        print(f"Please enter at least {minimum} item(s), separated by commas.")


def _number(prompt, default):
    while True:
        value = input(prompt).strip().replace(",", "").replace("$", "")
        if not value:
            return default
        if value.isascii() and value.isdigit():
            return int(value)
        print("Please enter a whole number of 0 or more, or press Enter to skip.")


def make_config(titles, skills, city, nearby, salary, years):
    """Translate plain text answers into the existing configuration format."""
    patterns = [_phrase(title) for title in titles]
    searches = [["linkedin", "United States", " remote", 40], ["indeed", "Remote", "", 40]]
    location = {}
    if city:
        nearby = [name for name in nearby if name.upper() not in US_STATE_CODES]
        local_names = [city.split(",", 1)[0].strip(), *nearby]
        local_patterns = [re.escape(name.lower()) for name in local_names]
        location = {
            "local_display_name": city,
            "local_metro_patterns": local_patterns,
            "jd_local_metro_pattern": r"\b(?:" + "|".join(local_patterns) + r")\b",
        }
        searches += [["linkedin", city, "", 20], ["indeed", city, "", 20]]
    return {
        "sources": {"search_terms": titles, "searches": searches},
        "location": location,
        "titles": {
            "target_patterns": patterns,
            "strong_target_patterns": [_strong_phrase(title) for title in titles],
            "exclude_seniority_patterns": [],
            "hn_role_pattern": "(" + "|".join(patterns) + ")",
        },
        "skills_and_scoring": {
            "core_strengths": [skill.lower() for skill in skills],
            "detect_stack_keywords": skills,
            "min_sync_score": 55,
        },
        "compensation": {"min_salary": salary},
        "experience": {"max_min_yoe": years},
    }


def main():
    load_env_files()
    # Resolved exactly as config.py does (no ~ expansion), so setup writes the file the pipeline will read
    path = os.path.abspath(os.environ.get("JOB_PIPELINE_CONFIG") or os.path.join(ROOT_DIR, "config.json"))
    if os.path.exists(path):
        print(f"Setup stopped: {path} already exists. Your settings were not changed.")
        return 1

    print("Let's set up your job search. Your answers are saved on this computer.")
    print("When you search, your titles and search location are sent to job sources.")
    print("This starter is for US-based searches. It searches remote jobs, with local jobs optional.")
    print("Use full job titles and skills as they appear in postings; separate each with a comma.")
    try:
        titles = _list("Job titles you want (e.g. Product Manager, Project Manager): ")
        skills = _list("Your skills (at least 3, e.g. planning, communication, budgeting): ", minimum=3)
        while True:
            city = input("US city and state for local jobs (e.g. Austin, TX), or Enter for remote only: ").strip()
            if not city or city.split(",", 1)[0].strip():
                break
            print("Please start with a city name, e.g. Austin, TX.")
        nearby = _list("Nearby city names without the state, separated by commas (e.g. Round Rock, Cedar Park), "
                       "or Enter for none: ", minimum=0) if city else []
        salary = _number("Minimum annual salary in USD (Enter for no minimum): ", 0)
        years = _number("Maximum years of experience a job may require (Enter for no limit): ", 99)
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. No settings were written.")
        return 1

    config = make_config(titles, skills, city, nearby, salary, years)
    try:
        with open(path, "x", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except FileExistsError:
        print(f"Setup stopped: {path} already exists. Your settings were not changed.")
        return 1
    except OSError as e:
        print(f"Could not write {path}: {e}", file=sys.stderr)
        return 1
    print(f"\nSaved your search to {path}.")
    print("Next: install dependencies, then run the first search shown in the README.")
    print("The starter uses exact title phrases and keyword scores; results may need tuning.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
