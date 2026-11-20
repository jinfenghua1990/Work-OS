"""Local owner login HTTP regression tests; never connect to Neon."""
import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import local_auth
import server


class LocalOwnerAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment=patch.dict(os.environ,{"WORKOS_AUTH_PASSWORD_HASH":local_auth.password_hash("a-long-test-secret-password")})
        cls.environment.start()
        cls.httpd=ThreadingHTTPServer(("127.0.0.1",0),server.Api)
        cls.port=cls.httpd.server_address[1]
        cls.thread=threading.Thread(target=cls.httpd.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(2)
        cls.environment.stop()

    def setUp(self):
        local_auth.reset_for_tests()

    def request(self,method,path,body=None,cookie=None,origin=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=5)
        headers={}
        if body is not None:
            headers["Content-Type"]="application/json"
            body=json.dumps(body)
        if cookie:
            headers["Cookie"]=cookie
        if origin is not None:
            headers["Origin"]=origin
        conn.request(method,path,body=body,headers=headers)
        response=conn.getresponse()
        raw=response.read()
        result=(response.status,json.loads(raw) if raw else {},
                response.getheader("Set-Cookie"))
        conn.close()
        return result

    def login(self):
        status,data,cookie=self.request("POST","/api/auth/login",
            {"password":"a-long-test-secret-password"},origin="http://127.0.0.1:5175")
        self.assertEqual(status,200,data)
        self.assertIn("HttpOnly",cookie)
        self.assertIn("SameSite=Strict",cookie)
        return cookie.split(";",1)[0]

    def test_business_routes_require_session(self):
        status,payload,_=self.request("GET","/api/health")
        self.assertEqual(status,401)
        self.assertEqual(payload["code"],"WORKOS_AUTH_REQUIRED")
        status,payload,_=self.request("GET","/api/auth/status")
        self.assertEqual(status,200)
        self.assertTrue(payload["requires_login"])
        self.assertFalse(payload["authenticated"])
        cookie=self.login()
        status,payload,_=self.request("GET","/api/health",cookie=cookie)
        self.assertEqual(status,200,payload)
        self.assertEqual(payload["status"],"ok")

    def test_logout_revokes_server_side_session(self):
        cookie=self.login()
        status,_,clear=self.request("POST","/api/auth/logout",{},cookie=cookie)
        self.assertEqual(status,200)
        self.assertIn("Max-Age=0",clear)
        self.assertEqual(self.request("GET","/api/health",cookie=cookie)[0],401)

    def test_cross_site_post_is_blocked_even_with_valid_cookie(self):
        cookie=self.login()
        status,_,_=self.request("POST","/api/invoices",{},cookie=cookie,
                               origin="https://attacker.example")
        self.assertEqual(status,403)

    def test_failed_password_is_throttled(self):
        for _ in range(local_auth.MAX_FAILED_ATTEMPTS):
            status,_,_=self.request("POST","/api/auth/login",{"password":"incorrect"})
            self.assertEqual(status,401)
        status,payload,_=self.request("POST","/api/auth/login",
                                        {"password":"a-long-test-secret-password"})
        self.assertEqual(status,401)
        self.assertIn("rate-limited",payload["error"])

    def test_remote_listener_requires_auth_gateway_not_available_here(self):
        with self.assertRaisesRegex(RuntimeError,"loopback"):
            local_auth.require_loopback_host("0.0.0.0")
        with self.assertRaisesRegex(ValueError,"12 characters"):
            local_auth.password_hash("short")

if __name__=="__main__":
    unittest.main()
