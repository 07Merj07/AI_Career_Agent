"""
selftest_job_search.py
======================

Offline tests for job_page_inspector.py and job_search.py.
No internet, Ollama or DDGS is needed: pages and searches are simulated.

Run from the Python folder:
    python selftest_job_search.py

Real pages must still be tested separately:
    python job_search.py --url <job page URL>
"""

import http.server
import json
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import requests

import job_page_inspector as inspector
import job_sources
import job_search as agent

TZ = inspector.TIMEZONE
NOW = datetime(2026, 11, 23, 19, 0, tzinfo=TZ)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


LONG_INTERN_TEXT = (
    "<p>You will clean and validate datasets, build weekly reports and dashboards, "
    "and support the analytics team with SQL and Python.</p>"
    "<ul><li>Final-year university student in a related field</li>"
    "<li>Good command of English</li><li>Knowledge of SQL and Excel</li></ul>"
)


def jsonld_page(*, title="Data Analyst Intern", company="Acme Analytics", date_posted=None,
                valid_through=None, locality="Istanbul", country="TR", description=LONG_INTERN_TEXT,
                extra=None, body_extra="", apply_link=True):
    posting = {
        "@context": "https://schema.org", "@type": "JobPosting", "title": title,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "description": description, "employmentType": "INTERN",
        "identifier": {"@type": "PropertyValue", "value": "REQ-1001"},
    }
    if date_posted is not None:
        posting["datePosted"] = date_posted
    if valid_through is not None:
        posting["validThrough"] = valid_through
    if locality or country:
        posting["jobLocation"] = {"@type": "Place", "address": {
            "@type": "PostalAddress", "addressLocality": locality, "addressCountry": country}}
    posting.update(extra or {})
    link = '<a href="/apply/1001">Apply now</a>' if apply_link else ""
    return (f"<html><head><title>{title}</title>"
            f'<script type="application/ld+json">{json.dumps(posting)}</script></head>'
            f"<body><h1>{title}</h1>{description}{link}{body_extra}</body></html>")


def screen_html(html, url="https://careers.acme.com/jobs/1001", *, max_age_hours=168, search_title=""):
    inspection = inspector.parse_job_page(html.encode("utf-8"), url, now=NOW, max_age_hours=max_age_hours)
    return agent.screen_job(inspection, {"title": search_title, "url": url, "snippet": ""})


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = requests.structures.CaseInsensitiveDict(
            headers if headers is not None else {"Content-Type": "text/html; charset=utf-8"})

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        pass


class FakeSession:
    """routes: url -> FakeResponse or Exception. Unknown URLs (e.g. robots.txt) -> 404."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        route = self.routes.get(url)
        if isinstance(route, Exception):
            raise route
        return route if route is not None else FakeResponse(404, b"", {"Content-Type": "text/plain"})


def fake_resolver(host, port):
    private = {"internal.example-corp.com": "10.0.0.5", "rebind.attacker.com": "127.0.0.1"}
    return [private.get(host, "93.184.216.34")]


class BaseTest(unittest.TestCase):
    def setUp(self):
        inspector.PER_HOST_DELAY_SECONDS = 0
        inspector._ROBOTS_CACHE.clear()
        patcher = mock.patch.object(inspector, "_resolve_addresses", side_effect=fake_resolver)
        patcher.start()
        self.addCleanup(patcher.stop)


# ------------------------------------------------------------
# 1. URL safety (test cases 18, 19, 20)
# ------------------------------------------------------------

class UrlSafetyTests(BaseTest):
    def test_unsafe_urls_rejected(self):
        for url in ("http://127.0.0.1/", "http://2130706433/", "http://0x7f000001/", "http://localhost/",
                    "http://169.254.169.254/latest", "http://[::1]/", "http://10.1.2.3/",
                    "https://user:pw@example.com/", "https://example.com:8080/", "ftp://example.com/",
                    "http://printer.local/", "http://internal.example-corp.com/",
                    "http://rebind.attacker.com/", "not a url", ""):
            with self.subTest(url=url):
                self.assertFalse(inspector.check_url(url)[0])

    def test_public_url_accepted(self):
        self.assertEqual(inspector.check_url("https://boards.greenhouse.io/acme/jobs/123"), (True, "ok"))

    def test_unsafe_url_never_requested(self):
        session = FakeSession({})
        result = inspector.inspect_job_page("http://2130706433/job", session=session, now=NOW)
        self.assertEqual(result["fetch_status"], "unsafe_url")
        self.assertEqual(session.calls, [])

    def test_redirect_to_unsafe_url_is_blocked(self):
        start = "https://jobs.acme.com/job/1"
        session = FakeSession({start: FakeResponse(302, b"", {"Location": "http://127.0.0.1/admin"})})
        result = inspector.inspect_job_page(start, session=session, now=NOW)
        self.assertEqual(result["fetch_status"], "unsafe_redirect")
        self.assertEqual(result["decision"], "UNVERIFIED")
        self.assertNotIn("http://127.0.0.1/admin", session.calls)

    def test_redirect_to_linkedin_not_followed(self):
        start = "https://jobs.acme.com/job/2"
        session = FakeSession({start: FakeResponse(301, b"", {"Location": "https://www.linkedin.com/jobs/view/123456789"})})
        result = inspector.inspect_job_page(start, session=session, now=NOW)
        self.assertEqual(result["fetch_status"], "skipped_by_policy")

    def test_too_many_redirects(self):
        routes = {f"https://jobs.acme.com/r{i}": FakeResponse(302, b"", {"Location": f"/r{i + 1}"}) for i in range(10)}
        result = inspector.inspect_job_page("https://jobs.acme.com/r0", session=FakeSession(routes), now=NOW)
        self.assertEqual(result["fetch_status"], "too_many_redirects")

    def test_timeout_and_network_error(self):
        url = "https://jobs.acme.com/slow"
        for error, expected in ((requests.Timeout("timed out"), "timeout"),
                                (requests.ConnectionError("connection reset"), "network_error")):
            with self.subTest(expected=expected):
                inspector._ROBOTS_CACHE.clear()
                result = inspector.inspect_job_page(url, session=FakeSession({url: error}), now=NOW)
                self.assertEqual(result["fetch_status"], expected)
                self.assertEqual(result["decision"], "UNVERIFIED")

    def test_connection_guard_blocks_private_peer(self):
        """Even if DNS checks were bypassed (rebinding), the socket check stops the request."""
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            session = inspector.create_session()
            with self.assertRaises(requests.RequestException) as caught:
                session.get(f"http://127.0.0.1:{server.server_address[1]}/", timeout=3)
            self.assertIn(inspector.BLOCKED_ADDRESS_MARKER, str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()

    def test_download_size_is_capped(self):
        url = "https://jobs.acme.com/big"
        big = jsonld_page(date_posted=iso(timedelta(days=1))) + "x" * (3 * 1024 * 1024)
        result = inspector.inspect_job_page(url, session=FakeSession({url: FakeResponse(200, big)}), now=NOW)
        self.assertTrue(result["page_truncated"])


# ------------------------------------------------------------
# 2. Inspector: page facts and contract
# ------------------------------------------------------------

class InspectorTests(BaseTest):
    def test_contract_has_all_fields_on_every_path(self):
        url = "https://jobs.acme.com/x"
        cases = [
            inspector.inspect_job_page("http://127.0.0.1/", now=NOW),
            inspector.inspect_job_page("https://www.linkedin.com/jobs/view/123456789", now=NOW),
            inspector.inspect_job_page(url, session=FakeSession({url: requests.Timeout()}), now=NOW),
            inspector.parse_job_page(b"<html></html>", url, now=NOW),
            inspector.parse_job_page(jsonld_page(date_posted=iso(timedelta(days=1))).encode(), url, now=NOW),
        ]
        for result in cases:
            self.assertEqual(set(result), set(inspector.RESULT_FIELDS))

    def test_full_fetch_with_jsonld(self):
        url = "https://jobs.acme.com/job/77"
        page = jsonld_page(date_posted=iso(timedelta(days=2)))
        result = inspector.inspect_job_page(url, session=FakeSession({url: FakeResponse(200, page)}), now=NOW)
        self.assertEqual(result["decision"], "OK")
        self.assertEqual(result["data_source"], "json_ld")
        self.assertEqual(result["company"], "Acme Analytics")
        self.assertEqual(result["job_identifier"], "REQ-1001")
        self.assertEqual(result["location_countries"], ["TR"])
        self.assertEqual(result["field_sources"]["publication_date"], "json_ld.datePosted")
        self.assertEqual(result["application_status"], "apply_link_found")

    def test_robots_disallow_is_respected(self):
        url = "https://jobs.acme.com/private/job/1"
        session = FakeSession({
            "https://jobs.acme.com/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /private/",
                                                             {"Content-Type": "text/plain"}),
            url: FakeResponse(200, jsonld_page(date_posted=iso(timedelta(days=1)))),
        })
        result = inspector.inspect_job_page(url, session=session, now=NOW)
        self.assertEqual(result["fetch_status"], "robots_disallowed")
        self.assertNotIn(url, session.calls)

    def test_blocked_and_js_pages_reported_honestly(self):
        url = "https://jobs.acme.com/job/5"
        blocked = inspector.inspect_job_page(url, session=FakeSession({url: FakeResponse(403, "Forbidden")}), now=NOW)
        self.assertEqual(blocked["fetch_status"], "blocked_by_site")
        captcha = inspector.parse_job_page(b"<html><body>Please verify you are human (captcha)</body></html>", url, now=NOW)
        self.assertEqual(captcha["fetch_status"], "blocked_by_site")
        js_only = inspector.parse_job_page(b"<html><body><div id='root'></div><script>app()</script></body></html>", url, now=NOW)
        self.assertEqual(js_only["fetch_status"], "javascript_required")

    def test_not_html_response(self):
        url = "https://jobs.acme.com/job.pdf"
        result = inspector.inspect_job_page(
            url, session=FakeSession({url: FakeResponse(200, b"%PDF", {"Content-Type": "application/pdf"})}), now=NOW)
        self.assertEqual(result["fetch_status"], "not_html")

    def test_html_fallback_without_jsonld(self):
        html = """<html><head><meta property="og:site_name" content="Acme Careers"></head><body>
        <nav>Home Jobs About</nav>
        <h1>Junior Data Analyst</h1>
        <div class="job-meta"><p>Location: Kadıköy, İstanbul</p><p>Çalışma şekli: Hibrit</p>
        <p>Yayın tarihi: 20 Kasım 2026</p><p>Son başvuru tarihi: 15.12.2026</p></div>
        <div class="job-description"><p>We are looking for a junior data analyst to build reports and
        dashboards with SQL, Python and Power BI. You will work with business teams and present results.</p>
        <ul><li>Bachelor's degree or final-year student</li><li>No previous experience is required.</li></ul></div>
        <a href="https://acme.com/apply?job=55">Başvur</a></body></html>"""
        result = inspector.parse_job_page(html.encode(), "https://acme.com/careers/junior-data-analyst", now=NOW)
        self.assertEqual(result["data_source"], "html_fallback")
        self.assertEqual(result["job_title"], "Junior Data Analyst")
        self.assertEqual(result["field_sources"]["job_title"], "html_h1")
        self.assertEqual(result["company"], "Acme Careers")
        self.assertIn("TR", result["location_countries"])
        self.assertEqual(result["work_model"], "hybrid")
        self.assertTrue(result["publication_date"].startswith("2026-11-20"))
        self.assertEqual(result["field_sources"]["publication_date"], "visible_text")
        self.assertTrue(result["application_deadline"].startswith("2026-12-15"))
        self.assertIn("Power BI", result["description_text"])
        self.assertEqual(result["decision"], "OK")

    def test_microdata_page(self):
        html = f"""<html><body><div itemscope itemtype="https://schema.org/JobPosting">
        <h1 itemprop="title">BI Intern</h1>
        <meta itemprop="datePosted" content="{iso(timedelta(days=1))}">
        <span itemprop="hiringOrganization" itemscope><span itemprop="name">Beta AŞ</span></span>
        <div itemprop="jobLocation" itemscope><span itemprop="addressLocality">Istanbul</span></div>
        <div itemprop="description">{LONG_INTERN_TEXT}</div></div></body></html>"""
        result = inspector.parse_job_page(html.encode(), "https://beta.com.tr/careers/1", now=NOW)
        self.assertEqual(result["data_source"], "microdata")
        self.assertEqual(result["job_title"], "BI Intern")
        self.assertEqual(result["company"], "Beta AŞ")
        self.assertEqual(result["decision"], "OK")

    def test_date_parsing(self):
        cases = {
            "20 Kasım 2026": "2026-11-20", "Nov 20, 2026": "2026-11-20", "20.11.2026": "2026-11-20",
            "2026-11-20": "2026-11-20", "3 gün önce": "2026-11-20", "Posted 3 days ago": "2026-11-20",
            "yesterday": "2026-11-22",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                dt, _ = inspector.parse_date_text(text, now=NOW)
                self.assertEqual(dt.date().isoformat(), expected)
        self.assertEqual(inspector.parse_date_text("05/06/2026", now=NOW), (None, None))   # ambiguous

    def test_closed_text_and_script_text(self):
        closed = jsonld_page(date_posted=iso(timedelta(days=1)),
                             body_extra="<div>This job is no longer available.</div>")
        self.assertEqual(inspector.parse_job_page(closed.encode(), "https://a.com/j", now=NOW)["decision"], "REJECTED")
        hidden = jsonld_page(date_posted=iso(timedelta(days=1)),
                             body_extra="<script>var msg='this job is closed';</script>")
        self.assertEqual(inspector.parse_job_page(hidden.encode(), "https://a.com/j", now=NOW)["decision"], "OK")

    def test_oracle_javascript_page_read_from_public_api(self):
        page_url = ("https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/"
                    "requisitions/job/38132")
        api_url = inspector.oracle_api_url(page_url)
        api_body = json.dumps({"items": [{
            "Id": 38132, "Title": "Quantitative Research Intern - Crisis Response",
            "ExternalPostedStartDate": iso(timedelta(days=2)), "ExternalPostedEndDate": "2026-12-10",
            "PrimaryLocation": "Istanbul, Turkey", "PrimaryLocationCountry": "TR",
            "WorkplaceType": "Hybrid",
            "ExternalDescriptionStr": LONG_INTERN_TEXT + "<script>alert(1)</script>"}]})
        session = FakeSession({
            page_url: FakeResponse(200, "<html><body><div id='app'></div><script>load()</script></body></html>"),
            api_url: FakeResponse(200, api_body, {"Content-Type": "application/json"}),
        })
        result = inspector.inspect_job_page(page_url, session=session, now=NOW)
        self.assertEqual(result["data_source"], "oracle_public_api")
        self.assertEqual(result["decision"], "OK", result["reason"])
        self.assertEqual(result["job_identifier"], "38132")
        self.assertEqual(result["work_model"], "hybrid")
        self.assertEqual(result["field_sources"]["publication_date"], "oracle_public_api.datePosted")
        record = agent.screen_job(result, {"title": "", "url": page_url, "snippet": ""})
        self.assertEqual(record["location_status"], "istanbul")
        self.assertEqual(record["screening_decision"], "CANDIDATE", record["screening_reason"])

    def test_oracle_api_failure_stays_unverified(self):
        page_url = "https://x.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/5"
        session = FakeSession({page_url: FakeResponse(200, "<html><body><div id='app'></div></body></html>")})
        result = inspector.inspect_job_page(page_url, session=session, now=NOW)
        self.assertEqual(result["fetch_status"], "javascript_required")

    def test_url_job_ids(self):
        self.assertEqual(inspector.extract_url_job_id(
            "https://www.linkedin.com/jobs/view/data-analyst-intern-at-acme-4012345678"), ("linkedin", "4012345678"))
        self.assertEqual(inspector.extract_url_job_id(
            "https://boards.greenhouse.io/acme/jobs/7012345"), ("greenhouse", "7012345"))
        ns, job_id = inspector.extract_url_job_id(
            "https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/job/38132")
        self.assertEqual(job_id, "38132")
        self.assertTrue(ns.startswith("oracle:"))


# ------------------------------------------------------------
# 3. Screening rules (test cases 2-13)
# ------------------------------------------------------------

class ScreeningTests(BaseTest):
    def test_jsonld_fresh_istanbul_intern_is_candidate(self):
        record = screen_html(jsonld_page(date_posted=iso(timedelta(days=2))))
        self.assertEqual(record["screening_decision"], "CANDIDATE", record["screening_reason"])

    def test_missing_date_is_unverified_date(self):
        record = screen_html(jsonld_page(date_posted=None))
        self.assertEqual(record["screening_decision"], "UNVERIFIED_DATE")

    def test_expired_deadline_rejected(self):
        record = screen_html(jsonld_page(date_posted=iso(timedelta(days=2)), valid_through="2026-11-22"))
        self.assertEqual(record["screening_decision"], "REJECTED")
        self.assertEqual(record["deadline_status"], "expired")

    def test_older_than_7_days_rejected(self):
        record = screen_html(jsonld_page(date_posted=iso(timedelta(days=9))))
        self.assertEqual(record["screening_decision"], "REJECTED")
        self.assertEqual(record["freshness"], "too_old")

    def test_24_hour_window_for_linkedin_rule(self):
        page = jsonld_page(date_posted=iso(timedelta(hours=30)))
        self.assertEqual(screen_html(page, max_age_hours=24)["screening_decision"], "REJECTED")
        self.assertEqual(screen_html(page, max_age_hours=168)["screening_decision"], "CANDIDATE")

    def test_linkedin_result_is_never_fetched(self):
        item = {"title": "Acme hiring Data Analyst Intern in Istanbul, Türkiye | LinkedIn",
                "url": "https://tr.linkedin.com/jobs/view/data-analyst-intern-at-acme-4012345678",
                "snippet": "", "search_queries": ["q"], "search_time_filter": "d"}
        record = agent.evaluate_search_item(item, inspect_fn=lambda *a, **k: self.fail("LinkedIn was fetched"))
        self.assertEqual(record["screening_decision"], "LINKEDIN_MANUAL_CHECK")
        self.assertEqual(record["company"], "Acme")
        self.assertEqual(record["location_status"], "istanbul")

    def test_istanbul_hybrid(self):
        page = jsonld_page(date_posted=iso(timedelta(days=1)), title="Data Analyst Intern (Hybrid)")
        record = screen_html(page)
        self.assertEqual(record["work_model"], "hybrid")
        self.assertEqual(record["screening_decision"], "CANDIDATE")

    def test_remote_open_to_turkey(self):
        page = jsonld_page(date_posted=iso(timedelta(days=1)), locality=None, country=None, extra={
            "jobLocationType": "TELECOMMUTE",
            "applicantLocationRequirements": [{"@type": "Country", "name": "Turkey"}]})
        record = screen_html(page)
        self.assertEqual(record["location_status"], "remote_turkey")
        self.assertEqual(record["screening_decision"], "CANDIDATE")

    def test_remote_not_open_to_turkey(self):
        page = jsonld_page(date_posted=iso(timedelta(days=1)), locality="Bangkok", country="TH", extra={
            "jobLocationType": "TELECOMMUTE",
            "applicantLocationRequirements": [{"@type": "Country", "name": "Thailand"}]})
        self.assertEqual(screen_html(page)["screening_decision"], "REJECTED")

    def test_remote_without_country_needs_review(self):
        page = jsonld_page(date_posted=iso(timedelta(days=1)), locality=None, country=None,
                           extra={"jobLocationType": "TELECOMMUTE"})
        record = screen_html(page)
        self.assertEqual(record["location_status"], "remote_country_unverified")
        self.assertEqual(record["screening_decision"], "MANUAL_REVIEW")

    def test_other_turkish_city_review_and_foreign_city_rejected(self):
        ankara = screen_html(jsonld_page(date_posted=iso(timedelta(days=1)), locality="Ankara"))
        self.assertEqual(ankara["screening_decision"], "MANUAL_REVIEW")
        berlin = screen_html(jsonld_page(date_posted=iso(timedelta(days=1)), locality="Berlin", country="DE"))
        self.assertEqual(berlin["screening_decision"], "REJECTED")

    def test_mandatory_two_years_rejected(self):
        description = LONG_INTERN_TEXT + "<h3>Requirements</h3><ul><li>At least 2 years of professional experience in data analysis</li></ul>"
        record = screen_html(jsonld_page(date_posted=iso(timedelta(days=1)), title="Junior Data Analyst",
                                         description=description))
        self.assertEqual(record["experience_status"], "reject_2plus_required")
        self.assertEqual(record["screening_decision"], "REJECTED")

    def test_preferred_five_years_not_rejected(self):
        description = LONG_INTERN_TEXT + "<p>5+ years of experience in analytics is a plus.</p>"
        record = screen_html(jsonld_page(date_posted=iso(timedelta(days=1)), description=description))
        self.assertEqual(record["experience_status"], "preferred_only")
        self.assertEqual(record["screening_decision"], "CANDIDATE")

    def test_one_year_required_goes_to_review(self):
        description = LONG_INTERN_TEXT + "<p>Minimum 1 year of experience is required.</p>"
        record = screen_html(jsonld_page(date_posted=iso(timedelta(days=1)), description=description))
        self.assertEqual(record["experience_status"], "review_1year_required")
        self.assertEqual(record["screening_decision"], "MANUAL_REVIEW")

    def test_senior_title_rejected(self):
        record = screen_html(jsonld_page(date_posted=iso(timedelta(days=1)), title="Senior Data Analyst"))
        self.assertEqual(record["screening_decision"], "REJECTED")


# ------------------------------------------------------------
# 4. Experience parser regressions (bugs found in the old version)
# ------------------------------------------------------------

class ExperienceTests(unittest.TestCase):
    CASES = {
        "We value 2 years of experience in data. Moreover, you will discover new tools.": "review_unclear_duration",
        "Requirements: Power BI knowledge is an advantage. At least 3 years of professional experience in analytics.": "reject_2plus_required",
        "1 year of experience with Excel. Must have 3 years of experience in analytics.": "reject_2plus_required",
        "En az 3 yıl tecrübe sahibi olmak zorunludur. Ekip çalışmasına yatkın olmak.": "reject_2plus_required",
        "Experience of at least 3 years is required for this analytics role in our team.": "reject_2plus_required",
        "No experience is required, we will train you on SQL, Python and our reporting tools.": "explicitly_not_required",
        "5+ years of experience is a plus. Strong communication skills and curiosity are important.": "preferred_only",
        "Aranan Nitelikler\n2 yıl deneyimli\nİleri düzey Excel bilgisi ve raporlama tecrübesi olan": "reject_2plus_required",
        "Tercih Sebebi\n3 yıl deneyim\nPower BI bilgisi\nSQL bilgisi ile veri analizi yapabilen adaylar": "preferred_only",
        "Mezuniyetine en az 1 yıl kalmış üniversite öğrencisi. SQL ve Excel bilgisi olan, analitik düşünebilen.": "no_requirement_found",
        "1-2 years of experience required in a data-related role, plus strong SQL and Excel skills.": "review_1year_required",
        "Two years of work experience is mandatory for this position. Please send your English CV.": "reject_2plus_required",
    }

    def test_cases(self):
        for text, expected in self.CASES.items():
            with self.subTest(text=text[:60]):
                self.assertEqual(agent.assess_experience(text)["status"], expected)

    def test_junior_without_requirement_needs_review(self):
        text = "We are looking for someone to build dashboards and reports with SQL, Python and Power BI."
        self.assertEqual(agent.assess_experience(text, is_intern=False)["status"], "review_no_requirement_junior")


# ------------------------------------------------------------
# 5. Search-result pre-filter (test cases 13-15)
# ------------------------------------------------------------

class PrefilterTests(unittest.TestCase):
    def check(self, title, url="https://careers.acme.com/job/1", snippet=""):
        return agent.prefilter_search_result({"title": title, "url": url, "snippet": snippet})[0]

    def test_accepted_titles(self):
        for title in ("Data Analyst Intern", "Data Intern", "Analytics Intern", "AI Engineer Intern",
                      "Research Assistant (Data)", "Junior Data Analyst", "Product Manager Intern",
                      "Veri Analisti Stajyeri", "İş Zekası Stajyer", "Yapay Zeka Stajyeri",
                      "2027 Summer Data Science Internship", "Business Intelligence Intern - Istanbul",
                      "A.I. Engineering Intern (Remote)", "Jr.Product Analyst", "Analiz & Raporlama Uzman Yardımcısı"):
            with self.subTest(title=title):
                self.assertTrue(self.check(title))

    def test_rejected_titles(self):
        for title in ("Senior Data Analyst", "Data Analytics Manager", "Lead Machine Learning Engineer",
                      "Data Analyst Graduate Programme", "Sales Intern", "Software Engineer Intern",
                      "Data Entry Intern", "Data Analyst II", "Data Analyst", "Kıdemli Veri Analisti"):
            with self.subTest(title=title):
                self.assertFalse(self.check(title))

    def test_course_and_generic_pages_rejected(self):
        self.assertFalse(self.check("Data Analyst Course with Internship | DataMites"))
        self.assertFalse(self.check("Data Analyst Resume Examples for Interns"))
        self.assertFalse(self.check("1,245 Data Analyst Intern jobs in Istanbul"))
        self.assertFalse(self.check("Data Analyst Intern", url="https://www.linkedin.com/jobs/search?keywords=data"))
        self.assertFalse(self.check("Junior Data Analyst Jobs in Berlin", url="https://x.com/jobs/berlin"))
        self.assertFalse(self.check("Data Intern", url="https://www.bing.com/aclick?ld=abc"))

    def test_linkedin_title_formats(self):
        tr = agent.parse_linkedin_title("Data Analyst - Paribu — İstanbul, Türkiye | LinkedIn İş İlanları")
        self.assertEqual((tr["title"], tr["company"], tr["location"]), ("Data Analyst", "Paribu", "İstanbul, Türkiye"))
        en = agent.parse_linkedin_title("Acme hiring BI Intern in Istanbul, Türkiye | LinkedIn")
        self.assertEqual((en["title"], en["company"]), ("BI Intern", "Acme"))
        plain = agent.parse_linkedin_title("Summer Internship 2027: Analytics & Insights - LinkedIn")
        self.assertEqual(plain["title"], "Summer Internship 2027: Analytics & Insights")
        self.assertTrue(self.check("Data Analyst Intern - Paribu — İstanbul, Türkiye | LinkedIn İş İlanları",
                                   url="https://tr.linkedin.com/jobs/view/data-analyst-intern-at-paribu-4400000001"))

    def test_level_found_in_snippet(self):
        self.assertTrue(self.check("Data Analytics - Acme", snippet="6-month internship in Istanbul"))


# ------------------------------------------------------------
# 6. Duplicates (test cases 16-17)
# ------------------------------------------------------------

def make_record(**fields):
    record = inspector.empty_result(fields.get("url", ""))
    record.update({"screening_decision": "CANDIDATE", "search_queries": []})
    record.update(fields)
    record["source_type"] = agent.source_type(record.get("url"))
    return record


class DuplicateTests(unittest.TestCase):
    def test_same_undp_job_across_sites_is_merged(self):
        official = make_record(
            url="https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/job/38132",
            company="United Nations Development Programme", job_title="Quantitative Research Intern",
            job_identifier="38132", location="Istanbul, Türkiye", location_cities=["Istanbul"],
            publication_date="2026-11-20T10:00:00+03:00", url_job_id="38132", url_job_id_namespace="oracle:x")
        aggregator = make_record(
            url="https://openopportunities.org/job/quantitative-research-intern-undp-38132",
            company="UNDP", job_title="Quantitative Research Intern - Istanbul", job_identifier="38132",
            location="Istanbul", location_cities=["Istanbul"], publication_date="2026-11-21T09:00:00+03:00")
        linkedin = make_record(
            url="https://www.linkedin.com/jobs/view/quantitative-research-intern-at-undp-4012345678",
            company="UNDP", job_title="Quantitative Research Intern", location="Istanbul, Türkiye",
            screening_decision="LINKEDIN_MANUAL_CHECK", url_job_id="4012345678", url_job_id_namespace="linkedin")
        merged = agent.deduplicate_jobs([aggregator, linkedin, official])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["url"], official["url"])
        self.assertEqual(merged[0]["duplicate_source_count"], 3)

    def test_two_positions_same_company_not_merged(self):
        a = make_record(url="https://boards.greenhouse.io/acme/jobs/111", company="Acme",
                        job_title="Data Analyst Intern", url_job_id="111", url_job_id_namespace="greenhouse",
                        location="Istanbul", publication_date="2026-11-20T10:00:00+03:00")
        b = make_record(url="https://boards.greenhouse.io/acme/jobs/222", company="Acme",
                        job_title="Data Analyst Intern", url_job_id="222", url_job_id_namespace="greenhouse",
                        location="Istanbul", publication_date="2026-11-20T11:00:00+03:00")
        c = make_record(url="https://acme.com/careers/bi-intern", company="Acme", job_title="BI Intern",
                        location="Istanbul", publication_date="2026-11-20T10:00:00+03:00")
        self.assertEqual(len(agent.deduplicate_jobs([a, b, c])), 3)

    def test_same_title_without_evidence_not_merged(self):
        a = make_record(url="https://site-a.com/job/1", company="Acme", job_title="Data Intern", location="Istanbul")
        b = make_record(url="https://site-b.com/job/9", company="Acme", job_title="Data Intern", location="Istanbul")
        self.assertEqual(len(agent.deduplicate_jobs([a, b])), 2)


# ------------------------------------------------------------
# 7. Search phase with fake Qwen and fake DDGS
# ------------------------------------------------------------

def fake_search(query, timelimit, max_results):
    return [{"title": f"Data Analyst Intern ({query})", "href": f"https://careers.acme.com/job/{abs(hash(query)) % 9999}",
             "body": "Istanbul internship"},
            {"title": "Same result", "href": "https://careers.acme.com/job/shared?utm_source=x", "body": ""}]


class SearchPhaseTests(unittest.TestCase):
    def setUp(self):
        agent.SECONDS_BETWEEN_SEARCHES = 0

    def test_llm_tool_loop_and_dedup_of_results(self):
        queries = iter(["data analyst intern Istanbul 2024", "veri analisti stajyer", "BI intern Istanbul",
                        "data analyst intern Istanbul", "AI intern Istanbul", "product analyst intern",
                        "data science intern"])

        def fake_chat(**kwargs):
            try:
                query = next(queries)
            except StopIteration:
                return SimpleNamespace(message=SimpleNamespace(role="assistant", content="DONE", tool_calls=None))
            call = SimpleNamespace(function=SimpleNamespace(name="web_search", arguments={"query": query}))
            return SimpleNamespace(message=SimpleNamespace(role="assistant", content="", tool_calls=[call]))

        state = agent.run_search_phase(use_llm=True, search_fn=fake_search, chat_fn=fake_chat)
        self.assertNotIn("2024", " ".join(state.queries))
        self.assertEqual(agent.clean_search_query("Data Analyst internship Istanbul 2026"),
                         "Data Analyst internship Istanbul")
        self.assertEqual(len(state.queries), len(set(q.casefold() for q in state.queries)))
        shared = [r for r in state.results if "shared" in r["url"]]
        self.assertEqual(len(shared), 1)
        self.assertGreater(len(shared[0]["search_queries"]), 1)

    def test_seed_queries_when_ollama_fails(self):
        def broken_chat(**kwargs):
            raise ConnectionError("Ollama is not running")

        state = agent.run_search_phase(use_llm=True, search_fn=fake_search, chat_fn=broken_chat)
        self.assertGreaterEqual(state.search_calls, agent.MIN_SEARCH_CALLS)

    def test_search_error_does_not_crash(self):
        def failing_search(query, timelimit, max_results):
            raise RuntimeError("rate limited")

        state = agent.run_search_phase(use_llm=False, search_fn=failing_search)
        self.assertTrue(state.errors)

    def test_empty_week_search_falls_back_to_month(self):
        calls = []

        def week_empty(query, timelimit, max_results):
            calls.append(timelimit)
            if timelimit == "w":
                raise RuntimeError("DDGSException: No results found.")
            return [{"title": "Data Analyst Intern", "href": "https://careers.acme.com/job/9", "body": ""}]

        state = agent.SearchState()
        summary = agent.run_web_search(state, "veri analisti stajyer", search_fn=week_empty)
        self.assertEqual(calls, ["w", "m"])
        self.assertEqual(summary["new_results"], 1)
        self.assertEqual(state.results[0]["search_time_filter"], "m")

    def test_linkedin_query_uses_one_day_filter(self):
        seen = {}

        def recording_search(query, timelimit, max_results):
            seen.setdefault(query, timelimit)   # first filter used for each query
            return []

        state = agent.SearchState()
        agent.run_web_search(state, "site:linkedin.com/jobs data analyst intern", search_fn=recording_search)
        agent.run_web_search(state, "data analyst intern Istanbul", search_fn=recording_search)
        self.assertEqual(list(seen.values()), ["d", "w"])


# ------------------------------------------------------------
# 8. Job API (JSearch) - offline, using the shape of a real response
# ------------------------------------------------------------

UNDP_TEXT = ("The Istanbul International Center for Private Sector in Development is one of UNDP's six "
             "Global Policy Centers. Collect and analyze trade data for target countries to support a market "
             "intelligence tool. Undertake quantitative analysis by applying statistical software such as R or "
             "Python and provide data management support including data collection, cleaning and processing. "
             "Be enrolled in the final academic year of a first university degree program. ") * 2


def jsearch_job(title, employer, posted_utc, posted_text, job_uid, *, location="Istanbul, Istanbul, Türkiye",
                description=LONG_INTERN_TEXT, publisher="LinkedIn"):
    slug = title.lower().replace(" ", "-")
    link = (f"https://tr.linkedin.com/jobs/view/{slug}-{job_uid}" if publisher == "LinkedIn"
            else f"https://www.kariyer.net/is-ilani/{slug}-{job_uid}")
    city, _, state = (location.split(", ") + ["", ""])[0], None, location.split(", ")[-1]
    return {"job_id": f"id-{job_uid}", "job_title": title, "employer_name": employer,
            "job_publisher": publisher, "job_apply_link": link,
            "apply_options": [{"apply_link": link, "publisher": publisher}],
            "job_description": description, "job_is_remote": None, "job_posted_at": posted_text,
            "job_posted_at_datetime_utc": posted_utc, "job_location": location,
            "job_city": city, "job_state": state, "job_country": "US", "job_uid": job_uid}


class FakeHttpResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class JSearchTests(unittest.TestCase):
    def test_conversion_and_screening(self):
        cases = [
            (jsearch_job("Summer Internship 2027: Analytics & Insights", "Procter & Gamble",
                         "2026-11-23T19:00:00.000Z", "today", "4484336941"), "CANDIDATE"),
            (jsearch_job("Financial Control Intern", "PepsiCo", "2026-11-18T23:00:00.000Z",
                         "5 days ago", "4490888435"), "REJECTED"),                 # too old for LinkedIn
            (jsearch_job("Data Analyst Intern", "Acme", "2026-11-20T09:00:00.000Z", "3 days ago",
                         "1", publisher="Kariyer.net"), "CANDIDATE"),             # 7-day rule
            (jsearch_job("Senior Data Analyst", "Acme", "2026-11-23T09:00:00.000Z", "today", "2"), "REJECTED"),
            (jsearch_job("Data Analyst Intern", "Acme", "2026-11-23T09:00:00.000Z", "today", "3",
                         location="Berlin, Germany"), "UNVERIFIED"),   # not Turkey -> never sent as a candidate
        ]
        for job, expected in cases:
            with self.subTest(title=job["job_title"], publisher=job["job_publisher"]):
                record = job_sources.jsearch_job_to_record(job, now=NOW)
                self.assertTrue(set(inspector.RESULT_FIELDS) <= set(record))   # contract + jsearch_job_id
                screened = agent.screen_job(record, {"title": job["job_title"], "url": record["url"], "snippet": ""})
                self.assertEqual(screened["screening_decision"], expected, screened["screening_reason"])

    def test_missing_description_is_fetched_from_details(self):
        import tempfile
        from pathlib import Path
        job_sources._working_url = None
        search_job = jsearch_job("Data Analyst Intern", "Acme", "2026-11-23T09:00:00.000Z", "today", "77",
                                 description="")
        other = jsearch_job("Sales Intern", "Beta", "2026-11-23T09:00:00.000Z", "today", "78", description="")
        detail_calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            if "job-details" in url:
                detail_calls.append(params["job_id"])
                return FakeHttpResponse(200, {"data": [{"job_description": LONG_INTERN_TEXT * 2}]})
            return FakeHttpResponse(200, {"data": {"jobs": [search_job, other]}})

        with mock.patch.dict("os.environ", {"RAPIDAPI_KEY": "k", "JSEARCH_URL": ""}), \
                mock.patch.object(job_sources, "USAGE_FILE", Path(tempfile.mkdtemp()) / "u.json"), \
                mock.patch.object(job_sources, "DEBUG_FILE", Path(tempfile.mkdtemp()) / "d.json"):
            result = job_sources.fetch_jsearch_jobs(
                http_get=fake_get, queries=("data analyst intern",),
                needs_details=lambda r: agent.assess_title(r["job_title"])["status"] == "ok")
        self.assertEqual(detail_calls, ["id-77"])            # the sales job was not worth a request
        descriptions = {r["job_title"]: r["description_text"] for r, _ in result["records"]}
        self.assertGreater(len(descriptions["Data Analyst Intern"]), 200)
        self.assertEqual(descriptions["Sales Intern"], "")
        job_sources._working_url = None

    def test_highlights_used_when_description_missing(self):
        job = jsearch_job("Data Analyst Intern", "Acme", "2026-11-23T09:00:00.000Z", "today", "79", description="")
        job["job_highlights"] = {"Qualifications": ["Minimum 3 years of experience is required."]}
        self.assertIn("3 years", job_sources.job_description(job))

    def test_wrong_country_field_is_ignored(self):
        record = job_sources.jsearch_job_to_record(jsearch_job(
            "Data Analyst Intern", "Acme", "2026-11-23T09:00:00.000Z", "today", "4"), now=NOW)
        self.assertEqual(record["location_countries"], ["TR"])

    def test_same_job_under_two_employer_names_is_merged(self):
        undp = jsearch_job("Quantitative Research Intern - Crisis Response", "UNDP Careers",
                           "2026-11-23T16:00:00.000Z", "today", "4492957125", description=UNDP_TEXT)
        icao = jsearch_job("Quantitative Research Intern - Crisis Response", "International Civil Aviation Organization",
                           "2026-11-23T16:00:00.000Z", "today", "4492962611", description=UNDP_TEXT)
        records = [agent.screen_job(job_sources.jsearch_job_to_record(job, now=NOW),
                                    {"title": job["job_title"], "url": job["job_apply_link"], "snippet": ""})
                   for job in (undp, icao)]
        self.assertEqual(len(agent.deduplicate_jobs(records)), 1)

    def test_fetch_without_key_is_skipped(self):
        with mock.patch.dict("os.environ", {"RAPIDAPI_KEY": ""}, clear=False), \
                mock.patch.object(job_sources, "ENV_FILE", job_sources.PROJECT_ROOT / "no-such.env"):
            result = job_sources.fetch_jsearch_jobs(http_get=lambda *a, **k: self.fail("called without key"))
        self.assertFalse(result["enabled"])

    def test_fetch_quota_and_errors(self):
        import tempfile
        from pathlib import Path
        usage = Path(tempfile.mkdtemp()) / "usage.json"
        payload = {"status": "OK", "data": {"jobs": [jsearch_job("Data Analyst Intern", "Acme",
                                                               "2026-11-23T09:00:00.000Z", "today", "9")]}}
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(params["query"])
            return FakeHttpResponse(200, payload)

        with mock.patch.dict("os.environ", {"RAPIDAPI_KEY": "test-key", "JSEARCH_MONTHLY_LIMIT": "3"}), \
                mock.patch.object(job_sources, "USAGE_FILE", usage):
            result = job_sources.fetch_jsearch_jobs(http_get=fake_get)
        self.assertEqual(len(calls), 3)                      # stopped at the monthly limit
        self.assertEqual(len(result["records"]), 1)          # same job only once
        self.assertTrue(any("limit" in e for e in result["errors"]))

        with mock.patch.dict("os.environ", {"RAPIDAPI_KEY": "bad-key"}), \
                mock.patch.object(job_sources, "USAGE_FILE", Path(tempfile.mkdtemp()) / "u.json"):
            bad = job_sources.fetch_jsearch_jobs(http_get=lambda *a, **k: FakeHttpResponse(403, {}))
        self.assertTrue(any("key rejected" in e for e in bad["errors"]))
        self.assertEqual(bad["records"], [])

    def test_new_endpoint_first_then_old_and_stop_on_404(self):
        import tempfile
        from pathlib import Path
        job_sources._working_url = None
        urls = []

        def only_old_works(url, params=None, headers=None, timeout=None):
            urls.append(url)
            if url.endswith("/search"):
                return FakeHttpResponse(200, {"data": {"jobs": []}})
            return FakeHttpResponse(404, {})

        with mock.patch.dict("os.environ", {"RAPIDAPI_KEY": "k", "JSEARCH_URL": ""}), \
                mock.patch.object(job_sources, "USAGE_FILE", Path(tempfile.mkdtemp()) / "u.json"):
            job_sources.fetch_jsearch_jobs(http_get=only_old_works, queries=("a", "b"))
        self.assertTrue(urls[0].endswith("/search-v2"))
        self.assertEqual(urls[1:], [urls[1]] * len(urls[1:]))     # remembered the working one
        job_sources._working_url = None
        urls.clear()

        with mock.patch.dict("os.environ", {"RAPIDAPI_KEY": "k", "JSEARCH_URL": ""}), \
                mock.patch.object(job_sources, "USAGE_FILE", Path(tempfile.mkdtemp()) / "u.json"):
            result = job_sources.fetch_jsearch_jobs(http_get=lambda url, **k: (urls.append(url), FakeHttpResponse(404, {}))[1],
                                                    queries=("a", "b", "c"))
        self.assertEqual(len(urls), 2)                                 # stopped after the first query
        self.assertTrue(any("Endpoint not found" in e for e in result["errors"]))
        job_sources._working_url = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
