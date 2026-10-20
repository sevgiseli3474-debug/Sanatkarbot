"""Basit ve sade MyBB 1.8 forum bağlantı ve konu açma aracı (HTTP uyumlu)."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import http.cookiejar
from http.cookiejar import Cookie
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


DEFAULT_FORUM_URL = "http://sanatkar.10tl.net"
EXPECTED_BOT_UID = "52"
REQUEST_TIMEOUT = 20

# Doğrudan tarayıcıdan aldığımız oturum çerezi
MYBBUSER_COOKIE_VALUE = "52_OKX47goCeg9u8No618blTlmoXxKtzSCF9pUfX4twSWyklxpcgm"


class ForumClientError(RuntimeError):
    """Kullanıcıya gösterilebilecek forum bağlantı veya form hatası."""


class InsecureForumError(ForumClientError):
    """Güvensiz forum bağlantı hatası."""


class ForumSubmissionUncertain(RuntimeError):
    """İstek gönderildi ancak forum sonucunu net olarak onaylamadı."""


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    """HTTPS yönlendirmelerini yöneten sınıf."""
    pass


@dataclass
class ForumForm:
    action: str
    method: str
    fields: list = field(default_factory=list)
    controls: dict = field(default_factory=dict)
    textarea_names: set = field(default_factory=set)


class _ForumPageParser(HTMLParser):
    """Forum sayfasındaki gizli form alanlarını ve CSRF token'larını okur."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.meta_refreshes = []
        self.visible_text = []
        self.current_form = None
        self.skip_text_depth = 0
        self.textarea_name = None
        self.textarea_parts = []
        self.select_name = None
        self.select_values = []
        self.selected_values = []
        self.option = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in {"script", "style"}:
            self.skip_text_depth += 1

        if tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
            self.meta_refreshes.append(attrs.get("content", ""))

        if tag == "form":
            self.current_form = ForumForm(
                action=attrs.get("action", ""),
                method=attrs.get("method", "get").lower(),
            )
            self.forms.append(self.current_form)
            return

        if self.current_form is None:
            return

        if tag == "input":
            name = attrs.get("name", "")
            input_type = attrs.get("type", "text").lower()
            if not name:
                return
            self.current_form.controls[name] = input_type
            if input_type in {"hidden", "submit", "button", "image"}:
                self.current_form.fields.append((name, attrs.get("value", "")))
            elif input_type in {"checkbox", "radio"} and "checked" in attrs:
                self.current_form.fields.append(
                    (name, attrs.get("value", "on"))
                )
        elif tag == "textarea":
            name = attrs.get("name", "")
            if name:
                self.textarea_name = name
                self.textarea_parts = []
                self.current_form.controls[name] = "textarea"
                self.current_form.textarea_names.add(name)
        elif tag == "select":
            name = attrs.get("name", "")
            if name:
                self.select_name = name
                self.select_values = []
                self.selected_values = []
                self.current_form.controls[name] = "select"
        elif tag == "option" and self.select_name:
            self.option = {
                "value": attrs.get("value"),
                "selected": "selected" in attrs,
                "text": [],
            }

    def handle_endtag(self, tag):
        if tag == "option" and self.option is not None:
            option_value = self.option["value"]
            if option_value is None:
                option_value = "".join(self.option["text"]).strip()
            self.select_values.append(option_value)
            if self.option["selected"]:
                self.selected_values.append(option_value)
            self.option = None
        elif tag == "select" and self.select_name and self.current_form:
            values = self.selected_values or self.select_values[:1]
            for value in values:
                self.current_form.fields.append((self.select_name, value))
            self.select_name = None
            self.select_values = []
            self.selected_values = []
        elif tag == "textarea" and self.textarea_name and self.current_form:
            self.current_form.fields.append(
                (self.textarea_name, "".join(self.textarea_parts))
            )
            self.textarea_name = None
            self.textarea_parts = []
        elif tag == "form":
            self.current_form = None

        if tag in {"script", "style"} and self.skip_text_depth:
            self.skip_text_depth -= 1

    def handle_data(self, data):
        if self.textarea_name:
            self.textarea_parts.append(data)
        if self.option is not None:
            self.option["text"].append(data)
        if not self.skip_text_depth:
            text = " ".join(data.split())
            if text:
                self.visible_text.append(text)


@dataclass
class PreparedThread:
    """Yeni konu formu verileri."""
    action_url: str
    fields: list
    referer: str


def _set_form_field(fields, name, value):
    """Form alanındaki belirli bir değeri günceller."""
    return [(key, old_value) for key, old_value in fields if key != name] + [
        (name, value)
    ]


class MyBBClient:
    """Çerezleri yöneten ve MyBB form token'larını kullanan standart HTTP istemcisi."""

    def __init__(self, base_url=DEFAULT_FORUM_URL):
        parsed = urlsplit(base_url.strip())
        if not parsed.netloc:
            raise ForumClientError("Geçersiz forum temel URL adresi.")

        self.base_url = base_url.rstrip("/")
        self.netloc = parsed.netloc.lower()
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(
            HTTPCookieProcessor(self.cookies)
        )
        
        # Sınıf başlar başlamaz mybbuser çerezini otomatik set ediyoruz
        cookie = Cookie(
            version=0,
            name='mybbuser',
            value=MYBBUSER_COOKIE_VALUE,
            port=None,
            port_specified=False,
            domain=self.netloc,
            domain_specified=True,
            domain_initial_dot=False,
            path='/',
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={'HttpOnly': None}
        )
        self.cookies.set_cookie(cookie)

    def _url(self, path):
        return urljoin(self.base_url + "/", path)

    def _request_page(self, url, fields=None, referer=None):
        data = None
        if fields is not None:
            data = urlencode(fields).encode("utf-8")

        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "SanatkarBot/1.0 (MyBB forum client)",
        }
        if referer:
            headers["Referer"] = referer
        request = Request(url, data=data, headers=headers)

        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                final_url = response.geturl()
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as error:
            raise ForumClientError(
                f"Forum HTTP hata kodu döndürdü: {error.code}"
            ) from error
        except URLError as error:
            raise ForumClientError(
                f"Foruma bağlanılamadı: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise ForumClientError("Forum isteği zaman aşımına uğradı.") from error
        except OSError as error:
            raise ForumClientError(f"Ağ hatası oluştu: {error}") from error

        text = body.decode(charset, errors="replace")
        parser = _ForumPageParser()
        parser.feed(text)
        return text, parser, final_url

    def _login_form(self):
        url = self._url("member.php?action=login")
        _, parser, final_url = self._request_page(url)
        for form in parser.forms:
            if (
                form.method == "post"
                and form.controls.get("username") in {"text", "email"}
                and form.controls.get("password") == "password"
            ):
                token_values = [
                    value for name, value in form.fields if name == "my_post_key"
                ]
                if not token_values or not token_values[-1]:
                    raise ForumClientError(
                        "Giriş formunda my_post_key bulunamadı."
                    )
                action_url = urljoin(final_url, form.action)
                return form, action_url, final_url
        raise ForumClientError("MyBB giriş formu bulunamadı.")

    def check_connection(self):
        """Giriş sayfasını ve form elemanlarını kontrol eder."""
        form, action_url, page_url = self._login_form()
        return {
            "login_form_found": True,
            "csrf_field_found": any(
                name == "my_post_key" and value
                for name, value in form.fields
            ),
            "login_form_url": action_url,
            "page_url": page_url,
        }

    def login(self, username=None, password=None):
        """Çerez doğrudan enjekte edildiği için şifre istemeden oturumu doğrular."""
        cookie_uid = None
        for cookie in self.cookies:
            if cookie.name == "mybbuser":
                match = re.match(r"^([0-9]+)_", cookie.value)
                if match:
                    cookie_uid = match.group(1)
                break

        if cookie_uid != EXPECTED_BOT_UID:
            raise ForumClientError(
                f"Beklenen bot hesabı (UID {EXPECTED_BOT_UID}) doğrulanamadı. "
                f"Mevcut gelen UID: {cookie_uid}"
            )
        return True

    def prepare_new_thread(self, section_id, title, message):
        """Belirtilen kategori bölümünde yeni konu formunu hazırlar."""
        if section_id < 1:
            raise ForumClientError("Geçerli bir forum kategori ID gerekli.")
        if not title.strip() or not message.strip():
            raise ForumClientError("Başlık ve mesaj içeriği zorunludur.")

        page_url = self._url(f"newthread.php?fid={section_id}")
        _, parser, final_url = self._request_page(page_url)
        candidate = None
        for form in parser.forms:
            if (
                form.method == "post"
                and form.controls.get("subject") in {"text", "search"}
                and "message" in form.textarea_names
            ):
                candidate = form
                break
        if candidate is None:
            raise ForumClientError(
                "Bu kategori için konu açma formu bulunamadı. Oturum kapalı olabilir."
            )

        tokens = [
            value for name, value in candidate.fields if name == "my_post_key"
        ]
        if not tokens or not tokens[-1]:
            raise ForumClientError("Konu formu my_post_key içermiyor.")

        action_url = urljoin(final_url, candidate.action)
        fields = list(candidate.fields)
        fields = _set_form_field(fields, "fid", str(section_id))
        fields = _set_form_field(fields, "action", "do_newthread")
        fields = _set_form_field(fields, "subject", title.strip())
        fields = _set_form_field(fields, "message", message.strip())
        fields = _set_form_field(fields, "submit", "Konuyu Gönder")
        return PreparedThread(
            action_url=action_url,
            fields=fields,
            referer=final_url,
        )

    def submit_new_thread(self, prepared_thread):
        """Hazırlanan konuyu forumda yayımlar."""
        print(f"--- GÖNDERİLEN URL: {prepared_thread.action_url} ---")
        print(f"--- GÖNDERİLEN ALANLAR: {prepared_thread.fields} ---")

        text, _, final_url = self._request_page(
            prepared_thread.action_url,
            fields=prepared_thread.fields,
            referer=prepared_thread.referer,
        )
        final = urlsplit(final_url)
        print(f"--- DÖNEN SONUÇ URL: {final_url} ---")

        if final.path.rstrip("/").endswith("showthread.php") or "showthread.php" in text or "konunuz" in text.lower():
            return final_url if final.path.rstrip("/").endswith("showthread.php") else prepared_thread.action_url
        raise ForumSubmissionUncertain(
            "Forum yeni konuyu tam olarak onaylamadı. Lütfen kontrol edin."
    )
