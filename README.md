# SanatkarBot

SanatkarBot reads RSS and Atom feeds and saves newly discovered items as drafts
in a local SQLite database. It checks saved item links and feed IDs so running
it again will not create duplicate drafts.

SanatkarBot does **not** use an AI API or publish automatically. A manual MyBB
publishing command is prepared, but it requires a working HTTPS forum connection
and an explicit confirmation for each post. You run RSS fetching manually.

## Requirements

- Python 3.9 or newer
- No third-party Python packages are required

SQLite, HTTP access, and XML parsing use Python's standard library.

## Set up

1. Open a terminal in the `SanatkarBot` folder.
2. Create a virtual environment:

   ```sh
   python -m venv .venv
   ```

3. Activate it:

   **macOS / Linux**

   ```sh
   source .venv/bin/activate
   ```

   **Windows**

   ```powershell
   .venv\Scripts\activate
   ```

4. Install the project requirements:

   ```sh
   pip install -r requirements.txt
   ```

   The requirements file is intentionally empty because this project only uses
   Python's built-in modules.

## Add feeds

Edit `feeds.txt` and add one RSS or Atom feed URL per line. Blank lines and
lines starting with `#` are ignored.

```text
# Add your forum or community RSS feed URLs below.
https://example.com/feed.xml
```

Add at least one real feed URL before fetching.

## Exclude titles

Add exact RSS item titles to `excluded_titles.txt`, one title per line. Matching
is case-sensitive; blank lines and lines starting with `#` are ignored.

Matching feed items are skipped on future fetches and hidden by `python main.py
list`. Existing matching rows are kept in SQLite, not deleted. Remove a title
from `excluded_titles.txt` to show its saved draft again.

## Match forum sections

`forum_sections.json` contains the Sanatkar section IDs and Turkish keywords
used by the local matcher. It includes IDs 4–54 and 59. IDs 1–3 (Genel Yönetim)
and 55–58 (Eğlence ve Müzik Türleri) are excluded; the program rejects them if
they are added to the configuration.

Each section has one numeric `id`, a `name`, and a list of `keywords`. For
example:

```json
{
  "sections": [
    {
      "id": 59,
      "name": "Sanat Haberleri",
      "keywords": ["sergi", "konser", "sanat haberleri"]
    }
  ]
}
```

The matcher gives title keyword matches two points and description matches one
point. It assigns one section ID only when one section has a unique highest
score. If there is a tie, the item is marked `ambiguous`; if nothing matches,
it is `unmatched`. In both cases, no target ID is assigned. Existing saved
items are re-matched on each fetch. Section matching itself never publishes
or deletes drafts.

## Forum connection

The MyBB client uses a cookie jar for the session and reads the current
`my_post_key` from each real form. It requires HTTPS with a valid certificate;
it refuses HTTP URLs, redirects to HTTP, and invalid TLS certificates. This
protects the account password, session cookies, and CSRF token from being sent
over an unencrypted connection.

The public Sanatkar forum currently says HTTPS is not configured and redirects
back to HTTP. Its `www` HTTPS address also has a certificate-name mismatch.
Because of this, `forum-check` stops safely before sending any account details.
Do not add the forum credentials to Secrets until HTTPS is configured correctly.

When the forum has valid HTTPS, add these two values in Replit's Secrets tool;
do not put them in source files:

- `SANATKARBOT_FORUM_USERNAME` — the dedicated bot account name, Boramete.
- `SANATKARBOT_FORUM_PASSWORD` — the password for that account.

After login, the client checks MyBB's `mybbuser` cookie and accepts only UID 52.
The cookie value and password are never printed. The username itself is not
hardcoded in the Python source.

The public forum address can be changed with the non-secret environment
variable `SANATKARBOT_FORUM_BASE_URL`. It defaults to
`https://sanatkar.10tl.net`.

Check the secure connection and login form without submitting a topic:

```sh
python main.py forum-check
```

If both Secrets are present and HTTPS is valid, this command also checks the
login and reports whether MyBB confirmed the session. It never creates a topic.

The manual publishing path is:

```sh
python main.py publish DRAFT_ID --confirm-publish
```

This command is not run automatically. It accepts only a saved draft with one
permitted, unambiguous section, and it rejects titles in
`excluded_titles.txt`. Before sending, it saves a unique publication reservation
in the existing SQLite database, keyed by the RSS item and a normalized exact
title. Uncertain requests remain blocked from retry so a timeout cannot cause
an accidental duplicate. A human must check the forum before resolving an
uncertain result.

Run the offline forum-client and duplicate-protection tests with:

```sh
python -m unittest discover -s . -p "test_*.py"
```

## Run

From the `SanatkarBot` folder, fetch new feed items:

```sh
python main.py
```

This is the same as:

```sh
python main.py fetch
```

List the latest 20 saved drafts:

```sh
python main.py list
```

Choose a different number of drafts to show:

```sh
python main.py list --limit 50
```

The program creates `sanatkarbot.db` in this folder the first time it runs.
Drafts remain there between runs. The database file is excluded from Git by
`.gitignore`.
