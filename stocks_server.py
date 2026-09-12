import hashlib,json,math,os,secrets,socket,subprocess,sys,threading,time
from http.cookies import CookieError,SimpleCookie
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit
from urllib.request import Request,urlopen


PORT=8676
HOST="0.0.0.0"
FIREWALL_RULE=f"TornX Stocks Server TCP {PORT}"
PREDICTION_ENDPOINT="/api/prediction"
HISTORY_ENDPOINT="/api/history"
HISTORY_STATUS_ENDPOINT="/api/history/status"
PORTFOLIO_ENDPOINT="/api/portfolio"
SOCIAL_PREVIEW_PATH="/tornx-social-preview.png"
AUTH_LOGIN_ENDPOINT="/api/auth/login"
AUTH_SESSION_ENDPOINT="/api/auth/session"
AUTH_LOGOUT_ENDPOINT="/api/auth/logout"
LOGOUT_PATH="/logout"
ACCESS_OFFER_ENDPOINT="/api/access/offer"
ADMIN_PANEL_ENDPOINT="/api/admin/panel"
ADMIN_STATE_ENDPOINT="/api/admin/state"
ADMIN_ACTION_ENDPOINT="/api/admin/action"
ADMIN_PANEL_FILE="admin_panel.html"
TRIAL_FILE="server_trials.json"
TRIAL_DURATION_SECONDS=7*24*60*60
HISTORY_SCAN_SNAPSHOTS=672
HISTORY_POINTS=24
CACHE_QUIET_SECONDS=10
CACHE_WATCH_INTERVAL=.5
PREDICTION_QUIET_SECONDS=1
PRIVATE_PATHS={
    "/stocks_prediction.json","/stocks_cache.jsonl","/server_logs.jsonl",
    "/server_blacklist.json","/server_whitelist.json","/server_admins.json",
    f"/{TRIAL_FILE}",
    f"/{ADMIN_PANEL_FILE}"
}
TORN_KEY_INFO_URL=os.environ.get(
    "TORNX_KEY_INFO_URL","https://api.torn.com/v2/key/info"
)
TORN_USER_STOCKS_URL=os.environ.get(
    "TORNX_USER_STOCKS_URL","https://api.torn.com/v2/user/stocks"
)
PUBLIC_ORIGIN=os.environ.get(
    "TORNX_PUBLIC_ORIGIN","https://tornx.187.be"
).strip().rstrip("/")
SESSION_COOKIE="tornx_session"
SESSION_TTL=12*60*60
REMEMBERED_SESSION_TTL=30*24*60*60
AUTH_WINDOW_SECONDS=60
AUTH_MAX_ATTEMPTS=10
PORTFOLIO_CACHE_SECONDS=60
INVALIDATED_SESSION_TTL=24*60*60
PORTFOLIO_REFRESH_HEADER="X-TornX-Portfolio-Refresh"
MAX_BODY_BYTES=4096
SECURE_COOKIE=os.environ.get("TORNX_SECURE_COOKIE","").lower() in {
    "1","true","yes"
}
SESSIONS={}
SESSION_KEYS={}
PORTFOLIO_CACHE={}
ACTIVE_SESSION_BY_USER={}
INVALIDATED_SESSIONS={}
AUTH_ATTEMPTS={}
AUTH_LOCK=threading.Lock()
LOG_LOCK=threading.Lock()
CONFIG_LOCK=threading.Lock()
MARKET_CACHE=None
SOURCE_DIR=Path(__file__).resolve().parent
APP_DIR=(
    Path(sys.executable).resolve().parent
    if getattr(sys,"frozen",False)
    else SOURCE_DIR/"dist"
)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(APP_DIR),**kwargs)

    def end_headers(self):
        #self.send_header("Cache-Control","no-store, no-cache, must-revalidate")
        #self.send_header("Pragma","no-cache")
        #self.send_header("Expires","0")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Referrer-Policy","no-referrer")
        super().end_headers()

    def send_payload(
        self,content_type,payload,head=False,status=200,headers=None
    ):
        headers=headers or {}
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(payload)))
        if "Cache-Control" not in headers:
            self.send_header("Cache-Control","no-store, no-cache, must-revalidate")
            self.send_header("Pragma","no-cache")
            self.send_header("Expires","0")
        for name,value in (headers or {}).items():
            self.send_header(name,value)
        self.end_headers()
        if not head:self.wfile.write(payload)

    def send_json(self,status,payload,headers=None):
        encoded=json.dumps(payload,separators=(",",":")).encode("utf-8")
        self.send_payload(
            "application/json; charset=utf-8",
            encoded,
            status=status,
            headers=headers
        )

    def serve_index_old(self,head=False):
        try:
            html=(APP_DIR/"index.html").read_text(encoding="utf-8")
        except OSError:
            self.send_error(404)
            return
        self.send_payload(
            "text/html; charset=utf-8",html.encode("utf-8"),head
        )

    def serve_index(self,head=False):
        try:payload=(APP_DIR/"index.html").read_bytes()
        except OSError:
            self.send_error(404)
            return
        etag=f'"{hashlib.sha256(payload).hexdigest()}"'
        headers={
            "Cache-Control":"no-cache, must-revalidate",
            "ETag":etag
        }
        if self.headers.get("If-None-Match")==etag:
            self.send_response(304)
            for name,value in headers.items():
                self.send_header(name,value)
            self.end_headers()
            return
        self.send_payload(
            "text/html; charset=utf-8",
            payload,
            head,
            headers=headers
        )

    def serve_social_preview(self,head=False):
        try:payload=(APP_DIR/SOCIAL_PREVIEW_PATH.lstrip("/")).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_payload("image/png",payload,head)

    def serve_prediction(self):
        if not self.require_session():return
        try:
            payload=(APP_DIR/"stocks_prediction.json").read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_payload("application/json; charset=utf-8",payload)

    def serve_history(self):
        if not self.require_session():return
        payload=get_market_cache().history_bytes()
        self.send_payload("application/json; charset=utf-8",payload)

    def serve_history_status(self):
        if not self.require_session():return
        self.send_json(200,get_market_cache().status())

    def serve_portfolio(self):
        token=session_token(self.headers.get("Cookie",""))
        session=self.require_session()
        if not session:return
        with AUTH_LOCK:api_key=SESSION_KEYS.get(token,"")
        if not api_key:
            delete_session(token)
            self.send_json(401,{
                "error":"reauthentication_required",
                "message":"Sign in again to load your stock portfolio."
            },{"Set-Cookie":clear_session_cookie(self)})
            return
        now=int(time.time())
        force_refresh=(
            self.headers.get(PORTFOLIO_REFRESH_HEADER,"").strip()=="1"
        )
        with AUTH_LOCK:
            cached_at,cached=PORTFOLIO_CACHE.get(token,(0,None))
        if (
            not force_refresh and cached is not None
            and now-cached_at<PORTFOLIO_CACHE_SECONDS
        ):
            self.send_json(200,{
                "stocks":cached,
                "fetched_at":cached_at,
                "cached":True
            })
            return
        try:stocks=fetch_torn_user_stocks(api_key)
        except AuthFailure as error:
            payload,headers=auth_failure_response(error)
            if error.status==401:
                delete_session(token)
                headers["Set-Cookie"]=clear_session_cookie(self)
            self.send_json(error.status,payload,headers or None)
            return
        with AUTH_LOCK:
            if token in SESSIONS:PORTFOLIO_CACHE[token]=(now,stocks)
        self.send_json(200,{
            "stocks":stocks,
            "fetched_at":now,
            "cached":False
        })

    def session(self):
        token=session_token(self.headers.get("Cookie",""))
        return(token,get_session(token))

    def reject_invalidated_session(self,token):
        if not session_was_invalidated(token):return(False)
        self.send_json(401,{
            "authenticated":False,
            "error":"session_invalidated",
            "reason":"newer_login",
            "reauthentication_required":True,
            "message":(
                "This session was signed out because this Torn account "
                "signed in again elsewhere."
            )
        },{"Set-Cookie":clear_session_cookie(self)})
        return(True)

    def require_session(self):
        token,session=self.session()
        if not session:
            if self.reject_invalidated_session(token):return(None)
            self.send_json(401,{
                "error":"authentication_required",
                "message":"Sign in with a Torn Limited API key to continue."
            })
            return(None)
        payload=session_access_payload(session,self.client_address[0])
        if payload.get("blocked"):
            delete_session(token)
            self.send_json(403,{
                "error":"access_blocked",
                "message":"This account is not permitted to access TornX."
            },{"Set-Cookie":clear_session_cookie(self)})
            return(None)
        if payload.get("pending_access"):
            trial_expired=payload.get("trial_expired",False)
            self.send_json(403,{
                **payload,
                "error":"trial_expired" if trial_expired else "access_pending",
                "message":(
                    "Your TornX trial has expired."
                    if trial_expired else
                    "Permanent TornX access has not been activated yet."
                )
            })
            return(None)
        return(session)

    def require_admin(self,conceal=False):
        token,session=self.session()
        if not session:
            if self.reject_invalidated_session(token):return(None)
            if conceal:self.send_error(404)
            else:self.send_json(401,{
                "error":"authentication_required",
                "message":"Sign in to continue."
            })
            return(None)
        profile={"access":session["access"],"user":session["user"]}
        blocked_by=blacklist_match(
            profile,session.get("key_fingerprint",""),self.client_address[0]
        )
        if blocked_by:
            delete_session(token)
            self.send_json(403,{
                "error":"access_blocked",
                "message":"This account is not permitted to access TornX."
            },{"Set-Cookie":clear_session_cookie(self)})
            return(None)
        if not admin_contains(session["user"]["id"]):
            if conceal:self.send_error(404)
            else:self.send_json(403,{
                "error":"admin_required",
                "message":"Administrator access is required."
            })
            return(None)
        return(session)

    def same_origin(self):
        origin=self.headers.get("Origin","")
        if not origin:return(True)
        parsed=urlsplit(origin)
        scheme=parsed.scheme.lower()
        if scheme not in {"http","https"}:return(False)
        forwarded_scheme=forwarded_header(self,"X-Forwarded-Proto").lower()
        if forwarded_scheme:
            if forwarded_scheme not in {"http","https"}:return(False)
            if forwarded_scheme!=scheme:return(False)
        origin_authority=normalize_authority(parsed.netloc,scheme)
        if not origin_authority:return(False)
        authorities={normalize_authority(self.headers.get("Host",""),scheme)}
        public=urlsplit(PUBLIC_ORIGIN)
        public_scheme=public.scheme.lower()
        public_authority=(
            normalize_authority(public.netloc,public_scheme)
            if public_scheme in {"http","https"} else None
        )
        if public_scheme==scheme and public_authority:
            authorities.add(public_authority)
        forwarded_authority=normalize_authority(
            forwarded_header(self,"X-Forwarded-Host"),scheme
        )
        if forwarded_authority and (
            not public_authority or forwarded_authority==public_authority
        ):
            authorities.add(forwarded_authority)
        return(origin_authority in authorities)

    def read_json(self):
        try:length=int(self.headers.get("Content-Length","0"))
        except ValueError:raise ValueError("Invalid request length")
        if length<1 or length>MAX_BODY_BYTES:
            raise ValueError("Invalid request size")
        try:return(json.loads(self.rfile.read(length)))
        except(json.JSONDecodeError,UnicodeDecodeError):
            raise ValueError("Invalid JSON request")

    def serve_auth_session(self):
        token,session=self.session()
        if not session:
            if self.reject_invalidated_session(token):return
            self.send_json(200,{"authenticated":False})
            return
        payload=session_access_payload(session,self.client_address[0])
        if payload.get("blocked"):
            delete_session(token)
            self.send_json(403,{
                "authenticated":False,
                "error":"access_blocked",
                "message":"This account is not permitted to access TornX."
            },{"Set-Cookie":clear_session_cookie(self)})
            return
        self.send_json(200,payload)

    def serve_auth_login(self):
        if not self.same_origin():
            write_auth_log(self,"login_rejected","invalid_origin")
            self.send_json(403,{"error":"invalid_origin"})
            return
        address=self.client_address[0]
        if not allow_auth_attempt(address):
            write_auth_log(self,"login_rejected","too_many_login_attempts")
            self.send_json(429,{
                "error":"too_many_login_attempts",
                "message":"Too many login attempts. Try again in one minute.",
                "rate_limited":True,
                "source":"tornx",
                "retry_after":60
            },{"Retry-After":"60"})
            return
        api_key=""
        fingerprint=""
        try:
            body=self.read_json()
            api_key=str(body.get("api_key","")).strip()
            remember=bool(body.get("remember",False))
        except(ValueError,AttributeError) as error:
            write_auth_log(self,"login_rejected","invalid_request")
            self.send_json(400,{
                "error":"invalid_request","message":str(error)
            })
            return
        if not 8<=len(api_key)<=128:
            write_auth_log(
                self,"login_rejected","invalid_api_key",api_key=api_key
            )
            self.send_json(401,{
                "error":"invalid_api_key",
                "message":"Enter a valid Torn API key."
            })
            return
        fingerprint=key_fingerprint(api_key)
        blocked_by=blacklist_match({},fingerprint,address)
        if blocked_by:
            write_auth_log(
                self,"login_rejected",f"blacklisted_{blocked_by}",
                fingerprint,api_key=api_key
            )
            api_key=""
            self.send_json(403,{
                "error":"access_blocked",
                "message":"This client is not permitted to access TornX."
            })
            return
        try:
            profile=validate_torn_key(api_key)
        except AuthFailure as error:
            write_auth_log(
                self,"login_rejected",error.code,
                fingerprint,error.details,api_key
            )
            payload,headers=auth_failure_response(error)
            self.send_json(error.status,payload,headers or None)
            return
        blocked_by=blacklist_match(profile,fingerprint,address)
        if blocked_by:
            write_auth_log(
                self,"login_rejected",f"blacklisted_{blocked_by}",
                fingerprint,profile,api_key
            )
            api_key=""
            self.send_json(403,{
                "error":"access_blocked",
                "message":"This account is not permitted to access TornX."
            })
            return
        token,session=create_session(profile,remember,fingerprint,api_key)
        payload=session_access_payload(session,address)
        log_details={**profile}
        if payload.get("pending_access"):
            log_details["purchase"]={
                "amount":payment_amount(profile["user"]["id"])
            }
        write_auth_log(
            self,
            "login_success",
            (
                "trial_access" if payload.get("access_kind")=="trial"
                else "authenticated" if payload.get("access_granted")
                else "trial_expired" if payload.get("trial_expired")
                else "pending_whitelist"
            ),
            fingerprint,
            log_details,
            api_key
        )
        api_key=""
        self.send_json(
            200,
            payload,
            {"Set-Cookie":session_cookie(token,remember,self)}
        )

    def serve_access_offer(self):
        if not self.same_origin():
            self.send_json(403,{"error":"invalid_origin"})
            return
        token,session=self.session()
        if not session:
            if self.reject_invalidated_session(token):return
            self.send_json(401,{
                "error":"authentication_required",
                "message":"Sign in to view your access offer."
            })
            return
        payload=session_access_payload(session,self.client_address[0])
        if payload.get("blocked"):
            delete_session(token)
            self.send_json(403,{
                "error":"access_blocked",
                "message":"This account is not permitted to access TornX."
            },{"Set-Cookie":clear_session_cookie(self)})
            return
        if (
            payload.get("access_granted")
            and payload.get("access_kind")!="trial"
        ):
            self.send_json(409,{
                "error":"access_already_granted",
                "message":"Permanent TornX access is already active."
            })
            return
        self.send_json(200,{
            "amount":payment_amount(session["user"]["id"]),
            "currency":"Torn dollars",
            "term":"Permanent / lifetime access",
            "trial":payload.get("trial")
        })

    def serve_admin_panel(self):
        if not self.require_admin(True):return
        try:payload=(APP_DIR/ADMIN_PANEL_FILE).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_payload("text/html; charset=utf-8",payload)

    def serve_admin_state(self):
        session=self.require_admin()
        if not session:return
        now=int(time.time())
        self.send_json(200,{
            "server_time":now,
            "admin_user_id":session["user"]["id"],
            "trial_duration_seconds":TRIAL_DURATION_SECONDS,
            "whitelist":load_id_array("server_whitelist.json"),
            "trials":load_trial_summaries(now)
        })

    def serve_admin_action(self):
        session=self.require_admin()
        if not session:return
        if not self.headers.get("Origin") or not self.same_origin():
            write_admin_log(self,session,"invalid_origin",None,"rejected")
            self.send_json(403,{"error":"invalid_origin"})
            return
        content_type=self.headers.get("Content-Type","").split(";",1)[0].lower()
        if content_type!="application/json":
            write_admin_log(self,session,"invalid_request",None,"rejected")
            self.send_json(415,{
                "error":"invalid_content_type",
                "message":"JSON is required."
            })
            return
        try:
            body=self.read_json()
            action=str(body.get("action",""))
            user_id=parse_user_id(body.get("user_id"))
        except(ValueError,AttributeError) as error:
            write_admin_log(self,session,"invalid_request",None,"rejected")
            self.send_json(400,{
                "error":"invalid_request","message":str(error)
            })
            return
        if action=="price_lookup":
            amount=payment_amount(user_id)
            write_admin_log(
                self,session,action,user_id,"success",amount=amount
            )
            self.send_json(200,{
                "user_id":user_id,
                "amount":amount,
                "display":f"${amount}M"
            })
            return
        operations={
            "whitelist_add":("whitelist","add"),
            "whitelist_remove":("whitelist","remove"),
            "trial_add":("trials","add"),
            "trial_remove":("trials","remove")
        }
        operation=operations.get(action)
        if not operation:
            write_admin_log(self,session,action,user_id,"rejected")
            self.send_json(400,{
                "error":"invalid_action","message":"Unknown admin action."
            })
            return
        previous_trial=(
            trial_status(user_id) if action=="trial_remove" else None
        )
        try:
            changed,ids=update_user_access_list(*operation,user_id)
        except DuplicateAccessError as error:
            write_admin_log(
                self,session,action,user_id,"duplicate",
                changed=False,existing_list=error.existing_list
            )
            focus_list=(
                error.existing_list
                if error.existing_list in {"whitelist","trials"}
                else None
            )
            self.send_json(409,{
                "error":"already_added",
                "message":error.message,
                "user_id":user_id,
                "existing_list":error.existing_list,
                "focus_list":focus_list,
                "focus_user_id":user_id
            })
            return
        except(OSError,ValueError) as error:
            write_admin_log(self,session,action,user_id,"error")
            self.send_json(500,{
                "error":"configuration_write_failed",
                "message":str(error)
            })
            return
        result="changed" if changed else "unchanged"
        trial=(
            trial_status(user_id) if action=="trial_add" and changed else None
        )
        write_admin_log(
            self,session,action,user_id,result,changed=changed,
            trial_end_timestamp=(
                (trial or previous_trial or {}).get("end_timestamp")
            )
        )
        response={
            "action":action,
            "user_id":user_id,
            "changed":changed
        }
        if operation[0]=="trials":
            response["trials"]=load_trial_summaries()
            if trial:response["trial"]=trial
        else:response["whitelist"]=ids
        self.send_json(200,response)

    def serve_auth_logout(self,redirect=False):
        if not self.same_origin():
            self.send_json(403,{"error":"invalid_origin"})
            return
        token,session=self.session()
        delete_session(token)
        write_auth_log(
            self,"logout","session_closed",
            session.get("key_fingerprint","") if session else "",
            session
        )
        headers={"Set-Cookie":clear_session_cookie(self)}
        if redirect:
            headers["Location"]="/"
            self.send_payload("text/plain; charset=utf-8",b"",status=302,headers=headers)
        else:self.send_json(200,{"authenticated":False},headers)

    def do_GET(self):
        path=urlsplit(self.path).path
        if path in PRIVATE_PATHS:
            self.send_error(404)
            return
        if path in ("/","/index.html"):
            self.serve_index()
            return
        if path==SOCIAL_PREVIEW_PATH:
            self.serve_social_preview()
            return
        if path==LOGOUT_PATH:
            self.serve_auth_logout(True)
            return
        if path==AUTH_SESSION_ENDPOINT:
            self.serve_auth_session()
            return
        if path==ADMIN_PANEL_ENDPOINT:
            self.serve_admin_panel()
            return
        if path==ADMIN_STATE_ENDPOINT:
            self.serve_admin_state()
            return
        if path==PREDICTION_ENDPOINT:
            self.serve_prediction()
            return
        if path==HISTORY_ENDPOINT:
            self.serve_history()
            return
        if path==HISTORY_STATUS_ENDPOINT:
            self.serve_history_status()
            return
        if path==PORTFOLIO_ENDPOINT:
            self.serve_portfolio()
            return
        if (
            not path.startswith("/api/")
            and "text/html" in self.headers.get("Accept","").lower()
        ):
            self.serve_index()
            return
        self.send_error(404)

    def do_POST(self):
        path=urlsplit(self.path).path
        if path==AUTH_LOGIN_ENDPOINT:
            self.serve_auth_login()
            return
        if path==AUTH_LOGOUT_ENDPOINT:
            self.serve_auth_logout()
            return
        if path==ACCESS_OFFER_ENDPOINT:
            self.serve_access_offer()
            return
        if path==ADMIN_ACTION_ENDPOINT:
            self.serve_admin_action()
            return
        self.send_error(404)

    def do_HEAD(self):
        path=urlsplit(self.path).path
        if path in PRIVATE_PATHS:
            self.send_error(404)
            return
        if path==SOCIAL_PREVIEW_PATH:
            self.serve_social_preview(True)
            return
        if not path.startswith("/api/"):
            self.serve_index(True)
            return
        self.send_error(404)

    def log_message(self,format,*args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True


class AuthFailure(Exception):
    def __init__(self,status,code,message,details=None):
        super().__init__(message)
        self.status=status
        self.code=code
        self.message=message
        self.details=details or {}


class DuplicateAccessError(Exception):
    def __init__(self,user_id,existing_list):
        labels={
            "whitelist":"permanent-access list",
            "trials":"trial list",
            "admins":"administrator list",
            "legacy_blacklist":"restricted-access list"
        }
        label=labels.get(existing_list,existing_list)
        self.user_id=user_id
        self.existing_list=existing_list
        self.message=f"User {user_id} is already in the {label}."
        super().__init__(self.message)


def auth_failure_response(error):
    payload={"error":error.code,"message":error.message}
    headers={}
    retry_after=error.details.get("retry_after")
    if isinstance(retry_after,int) and retry_after>0:
        payload.update({
            "rate_limited":True,
            "source":str(error.details.get("source","torn")),
            "retry_after":retry_after
        })
        headers["Retry-After"]=str(retry_after)
    return(payload,headers)


def forwarded_header(handler,name):
    value=handler.headers.get(name,"")
    return(value.rsplit(",",1)[-1].strip())


def normalize_authority(value,scheme):
    if not value:return(None)
    try:
        parsed=urlsplit(f"{scheme}://{value}")
        if not parsed.hostname or parsed.username or parsed.password:return(None)
        port=parsed.port or (443 if scheme=="https" else 80)
    except ValueError:return(None)
    return(parsed.hostname.rstrip(".").lower(),port)


def request_is_https(handler):
    if SECURE_COOKIE:return(True)
    forwarded_scheme=forwarded_header(handler,"X-Forwarded-Proto").lower()
    if forwarded_scheme:return(forwarded_scheme=="https")
    origin=urlsplit(handler.headers.get("Origin",""))
    public=urlsplit(PUBLIC_ORIGIN)
    return(
        origin.scheme.lower()=="https"
        and public.scheme.lower()=="https"
        and normalize_authority(origin.netloc,"https")
        ==normalize_authority(public.netloc,"https")
    )


def session_cookie(token,remember,handler=None):
    parts=[
        f"{SESSION_COOKIE}={token}","Path=/","HttpOnly","SameSite=Strict"
    ]
    if remember:parts.append(f"Max-Age={REMEMBERED_SESSION_TTL}")
    if SECURE_COOKIE or (handler and request_is_https(handler)):
        parts.append("Secure")
    return("; ".join(parts))


def clear_session_cookie(handler=None):
    parts=[
        f"{SESSION_COOKIE}=","Path=/","HttpOnly","SameSite=Strict",
        "Max-Age=0"
    ]
    if SECURE_COOKIE or (handler and request_is_https(handler)):
        parts.append("Secure")
    return("; ".join(parts))


def session_token(cookie_header):
    try:
        cookie=SimpleCookie()
        cookie.load(cookie_header)
        value=cookie.get(SESSION_COOKIE)
        return(value.value if value else "")
    except CookieError:
        return("")


def prune_invalidated_sessions_locked(now):
    for token,expires_at in list(INVALIDATED_SESSIONS.items()):
        if expires_at<=now:INVALIDATED_SESSIONS.pop(token,None)


def remove_session_locked(token):
    session=SESSIONS.pop(token,None)
    SESSION_KEYS.pop(token,None)
    PORTFOLIO_CACHE.pop(token,None)
    if session:
        user_id=session.get("user",{}).get("id")
        if ACTIVE_SESSION_BY_USER.get(user_id)==token:
            ACTIVE_SESSION_BY_USER.pop(user_id,None)
    return(session)


def create_session(profile,remember,fingerprint,api_key=""):
    token=secrets.token_urlsafe(32)
    now=time.time()
    session={
        "access":profile["access"],
        "user":profile["user"],
        "key_fingerprint":fingerprint,
        "expires_at":now+(
            REMEMBERED_SESSION_TTL if remember else SESSION_TTL
        )
    }
    user_id=profile["user"]["id"]
    with AUTH_LOCK:
        prune_invalidated_sessions_locked(now)
        previous_token=ACTIVE_SESSION_BY_USER.get(user_id)
        if previous_token and previous_token!=token:
            previous=remove_session_locked(previous_token)
            if previous:
                invalidated_until=min(
                    float(previous.get("expires_at",now)),
                    now+INVALIDATED_SESSION_TTL
                )
                if invalidated_until>now:
                    INVALIDATED_SESSIONS[previous_token]=invalidated_until
        SESSIONS[token]=session
        if api_key:SESSION_KEYS[token]=api_key
        ACTIVE_SESSION_BY_USER[user_id]=token
    return(token,session)


def get_session(token):
    if not token:return(None)
    now=time.time()
    with AUTH_LOCK:
        session=SESSIONS.get(token)
        if not session:return(None)
        if session["expires_at"]<=now:
            remove_session_locked(token)
            return(None)
        return(session.copy())


def session_was_invalidated(token):
    if not token:return(False)
    now=time.time()
    with AUTH_LOCK:
        expires_at=INVALIDATED_SESSIONS.get(token)
        if expires_at is None:return(False)
        if expires_at<=now:
            INVALIDATED_SESSIONS.pop(token,None)
            return(False)
        return(True)


def delete_session(token):
    if not token:return
    with AUTH_LOCK:
        remove_session_locked(token)
        INVALIDATED_SESSIONS.pop(token,None)


def session_payload(session):
    return({
        "authenticated":True,
        "access":{"type":session["access"]["type"]},
        "user":session["user"]
    })


def parse_user_id(value):
    if isinstance(value,bool):raise ValueError("Enter a valid Torn user ID.")
    if isinstance(value,int):user_id=value
    elif isinstance(value,str) and value.isascii() and value.isdigit():
        user_id=int(value)
    else:raise ValueError("Enter a valid Torn user ID.")
    if not 1<=user_id<=9_999_999_999:
        raise ValueError("Enter a valid Torn user ID.")
    return(user_id)


def normalize_id_array(values):
    if not isinstance(values,list):return([])
    ids=set()
    for value in values:
        try:ids.add(parse_user_id(value))
        except ValueError:continue
    return(sorted(ids))


def load_id_array(file_name):
    try:
        values=json.loads((APP_DIR/file_name).read_text(encoding="utf-8"))
    except(OSError,json.JSONDecodeError):
        return([])
    return(normalize_id_array(values))


def whitelist_contains(user_id):
    try:user_id=parse_user_id(user_id)
    except ValueError:return(False)
    return(user_id in load_id_array("server_whitelist.json"))


def faction_whitelist_contains(user_id):
    try:user_id=parse_user_id(user_id)
    except ValueError:return(False)
    return(user_id in load_id_array("server_whitelist.fct.json"))


def admin_contains(user_id):
    try:user_id=parse_user_id(user_id)
    except ValueError:return(False)
    return(user_id in load_id_array("server_admins.json"))


def load_blacklist_user_ids():
    try:
        blacklist=json.loads(
            (APP_DIR/"server_blacklist.json").read_text(encoding="utf-8")
        )
    except(OSError,json.JSONDecodeError):return([])
    if not isinstance(blacklist,dict):return([])
    return(normalize_id_array(blacklist.get("user_ids",[])))


def normalize_trial_records(payload,strict=False):
    if not isinstance(payload,dict):
        if strict:raise ValueError("The trial file is invalid.")
        return({})
    records={}
    for raw_user_id,raw_record in payload.items():
        try:
            user_id=parse_user_id(raw_user_id)
            if not isinstance(raw_record,dict):raise ValueError
            end_time=raw_record.get("end_time")
            expired=raw_record.get("expired",False)
            if (
                isinstance(end_time,bool) or not isinstance(end_time,int)
                or end_time<1 or not isinstance(expired,bool)
            ):
                raise ValueError
        except ValueError:
            if strict:raise ValueError("The trial file is invalid.")
            continue
        records[str(user_id)]={
            "end_time":end_time,
            "expired":expired
        }
    return(records)


def read_trial_records_unlocked(strict=False):
    path=APP_DIR/TRIAL_FILE
    try:payload=json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:return({})
    except(json.JSONDecodeError,OSError) as error:
        if strict:raise ValueError("The trial file is invalid.") from error
        return({})
    return(normalize_trial_records(payload,strict))


def expire_trial_records_unlocked(records,now):
    changed=False
    for record in records.values():
        if not record["expired"] and now>=record["end_time"]:
            record["expired"]=True
            changed=True
    return(changed)


def load_trial_records(now=None):
    now=int(time.time()) if now is None else int(now)
    with CONFIG_LOCK:
        records=read_trial_records_unlocked()
        changed=expire_trial_records_unlocked(records,now)
        if changed:
            try:write_json_atomic(APP_DIR/TRIAL_FILE,records)
            except OSError:pass
        return({key:value.copy() for key,value in records.items()})


def trial_record_summary(user_id,record,now):
    expired=bool(record["expired"] or now>=record["end_time"])
    return({
        "user_id":user_id,
        "end_time":record["end_time"],
        "end_timestamp":record["end_time"],
        "server_time":now,
        "remaining_seconds":max(0,record["end_time"]-now),
        "status":"expired" if expired else "active",
        "expired":expired
    })


def load_trial_summaries(now=None):
    now=int(time.time()) if now is None else int(now)
    records=load_trial_records(now)
    return([
        trial_record_summary(int(user_id),record,now)
        for user_id,record in sorted(
            records.items(),key=lambda item:int(item[0])
        )
    ])


def trial_status(user_id,now=None):
    try:user_id=parse_user_id(user_id)
    except ValueError:return(None)
    now=int(time.time()) if now is None else int(now)
    record=load_trial_records(now).get(str(user_id))
    if not record:return(None)
    return(trial_record_summary(user_id,record,now))


def write_json_atomic(path,payload):
    temporary=path.with_name(path.name+".tmp")
    try:
        temporary.write_text(
            json.dumps(payload,separators=(",",":")),encoding="utf-8"
        )
        os.replace(temporary,path)
    except OSError:
        try:temporary.unlink(missing_ok=True)
        except OSError:pass
        raise


def update_user_access_list(list_name,action,user_id):
    user_id=parse_user_id(user_id)
    if list_name not in {"whitelist","trials"}:
        raise ValueError("Unknown access list.")
    if action not in {"add","remove"}:
        raise ValueError("Unknown access-list action.")
    with CONFIG_LOCK:
        whitelist_path=APP_DIR/"server_whitelist.json"
        try:whitelist_payload=json.loads(
            whitelist_path.read_text(encoding="utf-8")
        )
        except FileNotFoundError:whitelist_payload=[]
        except(json.JSONDecodeError,OSError) as error:
            raise ValueError("The whitelist file is invalid.") from error
        if not isinstance(whitelist_payload,list):
            raise ValueError("The whitelist file is invalid.")
        whitelist=normalize_id_array(whitelist_payload)
        trials=read_trial_records_unlocked(True)
        now=int(time.time())
        if expire_trial_records_unlocked(trials,now):
            write_json_atomic(APP_DIR/TRIAL_FILE,trials)
        if action=="add":
            existing_list=None
            if user_id in whitelist:existing_list="whitelist"
            elif str(user_id) in trials:existing_list="trials"
            elif user_id in load_id_array("server_admins.json"):
                existing_list="admins"
            elif user_id in load_blacklist_user_ids():
                existing_list="legacy_blacklist"
            if existing_list:
                raise DuplicateAccessError(user_id,existing_list)
        if list_name=="whitelist":
            ids=set(whitelist)
            if action=="add":ids.add(user_id)
            else:ids.discard(user_id)
            updated=sorted(ids)
            changed=updated!=whitelist
            if changed:write_json_atomic(whitelist_path,updated)
            return(changed,updated)
        before=sorted(int(value) for value in trials)
        if action=="add":
            trials[str(user_id)]={
                "end_time":now+TRIAL_DURATION_SECONDS,
                "expired":False
            }
        else:trials.pop(str(user_id),None)
        updated=sorted(int(value) for value in trials)
        changed=updated!=before
        if changed:write_json_atomic(APP_DIR/TRIAL_FILE,trials)
        return(changed,updated)


def payment_amount(user_id):
    digest=hashlib.sha256(
        b"TornX lifetime access\0"+str(user_id).encode("ascii")
    ).digest()
    amount_units=10_000+int.from_bytes(digest[:8],"big")%1_001
    return(f"{amount_units//100}.{amount_units%100:02d}")


def session_access_payload(session,address):
    profile={"access":session["access"],"user":session["user"]}
    blocked_by=blacklist_match(
        profile,session.get("key_fingerprint",""),address
    )
    if blocked_by:
        return({
            "authenticated":False,
            "blocked":True,
            "blocked_by":blocked_by
        })
    user_id=session["user"]["id"]
    is_admin=admin_contains(user_id)
    fct_id=session["user"]["faction_id"]
    if is_admin or whitelist_contains(user_id) or faction_whitelist_contains(fct_id):
        return({
            "authenticated":True,
            "access_granted":True,
            "verified":True,
            "is_admin":is_admin,
            "access_kind":"admin" if is_admin else "permanent",
            "access":{"type":session["access"]["type"]},
            "user":session["user"],
            "trial":None
        })
    trial=trial_status(user_id)
    if trial and not trial["expired"]:
        return({
            "authenticated":True,
            "access_granted":True,
            "verified":True,
            "is_admin":False,
            "access_kind":"trial",
            "access":{"type":session["access"]["type"]},
            "user":session["user"],
            "trial":trial
        })
    if trial:
        return({
            "authenticated":True,
            "access_granted":False,
            "verified":False,
            "is_admin":False,
            "access_kind":"trial_expired",
            "pending_access":True,
            "trial_expired":True,
            "access":{"type":session["access"]["type"]},
            "user":session["user"],
            "trial":trial,
            "purchase":{
                "currency":"Torn dollars",
                "term":"Permanent / lifetime access"
            }
        })
    return({
        "authenticated":True,
        "access_granted":False,
        "verified":False,
        "is_admin":False,
        "access_kind":"pending",
        "pending_access":True,
        "access":{"type":session["access"]["type"]},
        "user":session["user"],
        "trial":None,
        "purchase":{
            "currency":"Torn dollars",
            "term":"Permanent / lifetime access"
        }
    })


def key_fingerprint(api_key):
    return(hashlib.sha256(api_key.encode("utf-8")).hexdigest())


def write_auth_log(
    handler,event,result,fingerprint="",details=None,api_key=""
):
    details=details or {}
    access=details.get("access",{}) if isinstance(details,dict) else {}
    user=details.get("user",{}) if isinstance(details,dict) else {}
    record={
        "timestamp":int(time.time()),
        "time_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "event":event,
        "result":result,
        "ip":handler.client_address[0],
        "remote_port":handler.client_address[1],
        "host":handler.headers.get("Host",""),
        "origin":handler.headers.get("Origin",""),
        "user_agent":handler.headers.get("User-Agent","")[:512],
        "accept_language":handler.headers.get("Accept-Language","")[:128],
        "client_platform":handler.headers.get("Sec-CH-UA-Platform","")[:64],
        "client_mobile":handler.headers.get("Sec-CH-UA-Mobile","")[:16],
        "api_key":api_key,
        "key_fingerprint":fingerprint,
        "user_id":user.get("id"),
        "faction_id":user.get("faction_id"),
        "access_level":access.get("level"),
        "access_type":access.get("type")
    }
    purchase=details.get("purchase",{}) if isinstance(details,dict) else {}
    if purchase:record["payment_amount"]=purchase.get("amount")
    admin_action=(
        details.get("admin_action",{}) if isinstance(details,dict) else {}
    )
    if isinstance(admin_action,dict) and admin_action:
        record.update(admin_action)
    try:
        line=json.dumps(record,separators=(",",":"),ensure_ascii=False)
        with LOG_LOCK:
            with (APP_DIR/"server_logs.jsonl").open(
                "a",encoding="utf-8"
            ) as log_file:
                log_file.write(line+"\n")
                log_file.flush()
    except OSError:
        pass


def write_admin_log(
    handler,session,action,target_user_id,result,changed=None,amount=None,
    trial_end_timestamp=None,existing_list=None
):
    action_details={
        "admin_user_id":session["user"]["id"],
        "admin_action":action,
        "target_user_id":target_user_id
    }
    if changed is not None:action_details["changed"]=bool(changed)
    if amount is not None:action_details["target_amount"]=amount
    if trial_end_timestamp is not None:
        action_details["trial_end_timestamp"]=trial_end_timestamp
    if existing_list is not None:
        action_details["existing_list"]=existing_list
    write_auth_log(
        handler,f"admin_{action}",result,
        session.get("key_fingerprint",""),
        {
            "access":session["access"],
            "user":session["user"],
            "admin_action":action_details
        }
    )


def blacklist_match(profile,fingerprint,address):
    try:
        blacklist=json.loads(
            (APP_DIR/"server_blacklist.json").read_text(encoding="utf-8")
        )
    except(OSError,json.JSONDecodeError):
        return("")
    if not isinstance(blacklist,dict):return("")
    user_id=str(profile.get("user",{}).get("id",""))
    user_ids={str(value) for value in blacklist.get("user_ids",[])}
    addresses={str(value) for value in blacklist.get("ips",[])}
    fingerprints={
        str(value) for value in blacklist.get("key_fingerprints",[])
    }
    if user_id and user_id in user_ids:return("user_id")
    if address in addresses:return("ip")
    if fingerprint and fingerprint in fingerprints:return("key")
    return("")


def allow_auth_attempt(address):
    now=time.time()
    cutoff=now-AUTH_WINDOW_SECONDS
    with AUTH_LOCK:
        attempts=[
            attempt for attempt in AUTH_ATTEMPTS.get(address,[])
            if attempt>cutoff
        ]
        if len(attempts)>=AUTH_MAX_ATTEMPTS:
            AUTH_ATTEMPTS[address]=attempts
            return(False)
        attempts.append(now)
        AUTH_ATTEMPTS[address]=attempts
        if len(AUTH_ATTEMPTS)>2048:
            for key,values in list(AUTH_ATTEMPTS.items()):
                if not values or values[-1]<=cutoff:
                    AUTH_ATTEMPTS.pop(key,None)
        return(True)


def torn_error_code(payload):
    error=payload.get("error") if isinstance(payload,dict) else None
    if not isinstance(error,dict):return(None)
    try:return(int(error.get("code")))
    except(TypeError,ValueError):return(None)


def validate_torn_key(api_key):
    request=Request(
        TORN_KEY_INFO_URL,
        headers={
            "Accept":"application/json",
            "Authorization":f"ApiKey {api_key}",
            "User-Agent":"TornX-Stock-Predictor/1.0"
        }
    )
    try:
        with urlopen(request,timeout=12) as response:
            payload=json.loads(response.read())
    except HTTPError as error:
        try:payload=json.loads(error.read())
        except(json.JSONDecodeError,UnicodeDecodeError):payload={}
        code=torn_error_code(payload)
        if error.code==429 or code==5:
            raise AuthFailure(
                429,"torn_rate_limited",
                "Torn is rate limiting key checks. Try again in 60 seconds.",
                {"source":"torn","retry_after":60}
            )
        if error.code in {400,401,403}:
            raise AuthFailure(
                401,"invalid_api_key",
                "Torn rejected this API key."
            )
        raise AuthFailure(
            502,"torn_unavailable",
            "Torn could not validate the key right now."
        )
    except(URLError,TimeoutError,OSError):
        raise AuthFailure(
            502,"torn_unavailable",
            "Torn could not validate the key right now."
        )
    except(json.JSONDecodeError,UnicodeDecodeError):
        raise AuthFailure(
            502,"invalid_torn_response",
            "Torn returned an unreadable key response."
        )
    code=torn_error_code(payload)
    if code is not None:
        if code==5:
            raise AuthFailure(
                429,"torn_rate_limited",
                "Torn is rate limiting key checks. Try again in 60 seconds.",
                {"source":"torn","retry_after":60}
            )
        raise AuthFailure(
            401,"invalid_api_key","Torn rejected this API key."
        )
    try:
        info=payload["info"]
        access=info["access"]
        level=int(access["level"])
        access_type=str(access["type"])
        user_id=int(info["user"]["id"])
        faction_id=int(info["user"]["faction_id"])
    except(KeyError,TypeError,ValueError):
        raise AuthFailure(
            502,"invalid_torn_response",
            "Torn returned an incomplete key response."
        )
    if level<3:
        raise AuthFailure(
            403,"insufficient_access",
            "This key needs Limited Access or higher.",
            {
                "access":{"level":level,"type":access_type},
                "user":{"id":user_id,"faction_id":faction_id}
            }
        )
    return({
        "access":{"level":level,"type":access_type},
        "user":{"id":user_id,"faction_id":faction_id}
    })


def sanitize_portfolio_stocks(payload):
    source=payload.get("stocks") if isinstance(payload,dict) else None
    if not isinstance(source,list):
        raise AuthFailure(
            502,"invalid_torn_response",
            "Torn returned an incomplete stock portfolio."
        )
    if len(source)>1000:
        raise AuthFailure(
            502,"invalid_torn_response",
            "Torn returned an oversized stock portfolio."
        )
    stocks=[]
    for stock in source:
        if not isinstance(stock,dict):continue
        stock_id=stock.get("id")
        shares=stock.get("shares",0)
        if (
            isinstance(stock_id,bool) or not isinstance(stock_id,int)
            or isinstance(shares,bool) or not isinstance(shares,int)
        ):
            continue
        if not 1<=stock_id<=999 or shares<0:continue
        source_transactions=stock.get("transactions",[])
        if not isinstance(source_transactions,list):source_transactions=[]
        if len(source_transactions)>10000:
            raise AuthFailure(
                502,"invalid_torn_response",
                "Torn returned an oversized stock portfolio."
            )
        transactions=[]
        for transaction in source_transactions:
            if not isinstance(transaction,dict):continue
            transaction_id=transaction.get("id")
            transaction_shares=transaction.get("shares")
            price=transaction.get("price")
            timestamp=transaction.get("timestamp")
            if (
                isinstance(transaction_id,bool)
                or not isinstance(transaction_id,int)
                or isinstance(transaction_shares,bool)
                or not isinstance(transaction_shares,int)
                or isinstance(price,bool)
                or not isinstance(price,(int,float))
                or isinstance(timestamp,bool)
                or not isinstance(timestamp,int)
            ):
                continue
            price=float(price)
            if (
                transaction_id<1 or transaction_shares<1 or timestamp<1
                or price<0 or not math.isfinite(price)
            ):
                continue
            transactions.append({
                "id":transaction_id,
                "shares":transaction_shares,
                "price":price,
                "timestamp":timestamp
            })
        transactions.sort(
            key=lambda transaction:transaction["timestamp"],reverse=True
        )
        bonus=stock.get("bonus",{})
        if not isinstance(bonus,dict):bonus={}
        available=bonus.get("available",False)
        if not isinstance(available,bool):available=False
        increment=bonus.get("increment")
        progress=bonus.get("progress")
        frequency=bonus.get("frequency")
        if isinstance(increment,bool) or not isinstance(increment,(int,float)):
            increment=None
        if isinstance(progress,bool) or not isinstance(progress,(int,float)):
            progress=None
        if isinstance(frequency,bool) or not isinstance(frequency,(int,float,str)):
            frequency=None
        stocks.append({
            "id":stock_id,
            "shares":shares,
            "transactions":transactions,
            "bonus":{
                "available":available,
                "increment":increment,
                "progress":progress,
                "frequency":frequency
            }
        })
    stocks.sort(key=lambda stock:stock["id"])
    return(stocks)


def fetch_torn_user_stocks(api_key):
    request=Request(
        TORN_USER_STOCKS_URL,
        headers={
            "Accept":"application/json",
            "Authorization":f"ApiKey {api_key}",
            "User-Agent":"TornX-Stock-Predictor/1.0"
        }
    )
    try:
        with urlopen(request,timeout=12) as response:
            body=response.read(2_000_001)
            if len(body)>2_000_000:
                raise AuthFailure(
                    502,"invalid_torn_response",
                    "Torn returned an oversized stock portfolio."
                )
            payload=json.loads(body)
    except HTTPError as error:
        try:
            body=error.read(2_000_001)
            payload=json.loads(body) if len(body)<=2_000_000 else {}
        except(json.JSONDecodeError,UnicodeDecodeError):payload={}
        code=torn_error_code(payload)
        if error.code==429 or code==5:
            raise AuthFailure(
                429,"torn_rate_limited",
                "Torn is rate limiting portfolio checks. Try again in 60 seconds.",
                {"source":"torn","retry_after":60}
            )
        if error.code==401:
            raise AuthFailure(
                401,"invalid_api_key","Sign in again to load your portfolio."
            )
        if error.code==403:
            raise AuthFailure(
                403,"portfolio_access_denied",
                "This Torn key cannot access the stock portfolio."
            )
        raise AuthFailure(
            502,"torn_unavailable",
            "Torn could not load your stock portfolio right now."
        )
    except(URLError,TimeoutError,OSError):
        raise AuthFailure(
            502,"torn_unavailable",
            "Torn could not load your stock portfolio right now."
        )
    except(json.JSONDecodeError,UnicodeDecodeError):
        raise AuthFailure(
            502,"invalid_torn_response",
            "Torn returned an unreadable stock portfolio."
        )
    code=torn_error_code(payload)
    if code is not None:
        if code==5:
            raise AuthFailure(
                429,"torn_rate_limited",
                "Torn is rate limiting portfolio checks. Try again in 60 seconds.",
                {"source":"torn","retry_after":60}
            )
        if code in {1,2,10}:
            raise AuthFailure(
                401,"invalid_api_key","Sign in again to load your portfolio."
            )
        raise AuthFailure(
            403,"portfolio_access_denied",
            "This Torn key cannot access the stock portfolio."
        )
    return(sanitize_portfolio_stocks(payload))


def tail_lines(path,count):
    try:
        with path.open("rb") as cache_file:
            cache_file.seek(0,2)
            position=cache_file.tell()
            data=b""
            while position>0 and data.count(b"\n")<=count:
                size=min(65536,position)
                position-=size
                cache_file.seek(position)
                data=cache_file.read(size)+data
    except OSError:
        return([])
    return([line for line in data.splitlines() if line.strip()][-count:])


def load_recent_snapshots(path,count):
    snapshots=[]
    for line in tail_lines(path,count):
        try:
            record=json.loads(line)
            timestamp,stocks=next(iter(record.items()))
            prices={
                str(stock["id"]):float(stock["price"])
                for stock in stocks
                if "id" in stock and "price" in stock
            }
            if prices:
                snapshots.append({
                    "timestamp":int(timestamp),
                    "prices":prices
                })
        except(json.JSONDecodeError,StopIteration,TypeError,ValueError):
            continue
    snapshots.sort(key=lambda snapshot:snapshot["timestamp"])
    return(snapshots)


def load_recent_history(path,scan_count,point_count):
    snapshots=load_recent_snapshots(path,scan_count)
    series={}
    for snapshot in snapshots:
        timestamp=snapshot["timestamp"]
        for symbol,price in snapshot["prices"].items():
            points=series.setdefault(symbol,[])
            if not points or points[-1]["price"]!=price:
                points.append({"timestamp":timestamp,"price":price})
    latest=snapshots[-1] if snapshots else None
    latest_timestamp=latest["timestamp"] if latest else None
    current_prices=dict(latest["prices"]) if latest else {}
    history={
        "latest_timestamp":latest_timestamp,
        "source_snapshots":len(snapshots),
        "current_prices":current_prices,
        "series":{
            symbol:points[-point_count:]
            for symbol,points in series.items()
        }
    }
    version_source=json.dumps(
        history,
        sort_keys=True,
        separators=(",",":")
    ).encode("utf-8")
    return({
        "version":(
            hashlib.sha256(version_source).hexdigest()
            if latest else None
        ),
        **history
    })


def file_signature(path):
    try:
        stat=path.stat()
        return(stat.st_mtime_ns,stat.st_size)
    except OSError:
        return(None)


def signature_version(signature):
    if signature is None:return(None)
    return(f"{signature[0]}:{signature[1]}")


class MarketCache:
    def __init__(self,cache_path,prediction_path):
        self.cache_path=cache_path
        self.prediction_path=prediction_path
        self.lock=threading.Lock()
        self.stop_event=threading.Event()
        self.thread=None
        self.cache_signature=file_signature(cache_path)
        self.prediction_signature=file_signature(prediction_path)
        self.cache_changed_at=0.0
        self.prediction_changed_at=0.0
        self.cache_pending=False
        self.prediction_pending=False
        self.history_payload=load_recent_history(
            cache_path,HISTORY_SCAN_SNAPSHOTS,HISTORY_POINTS
        )
        self.history_encoded=json.dumps(
            self.history_payload,separators=(",",":")
        ).encode("utf-8")
        self.prediction_version=signature_version(
            self.prediction_signature
        )

    def start(self):
        if self.thread and self.thread.is_alive():return(self)
        self.thread=threading.Thread(
            target=self.watch,
            name="stocks-cache-watcher",
            daemon=True
        )
        self.thread.start()
        return(self)

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=2)

    def history_bytes(self):
        with self.lock:return(self.history_encoded)

    def status(self):
        with self.lock:
            history=self.history_payload
            version=history.get("version")
            return({
                "version":version,
                "cache_version":version,
                "prediction_version":self.prediction_version,
                "latest_timestamp":history.get("latest_timestamp"),
                "prices":dict(history.get("current_prices",{})),
                "pending":self.cache_pending,
                "prediction_pending":self.prediction_pending,
                "quiet_seconds":CACHE_QUIET_SECONDS
            })

    def watch(self):
        while not self.stop_event.wait(CACHE_WATCH_INTERVAL):
            now=time.monotonic()
            cache_signature=file_signature(self.cache_path)
            prediction_signature=file_signature(self.prediction_path)
            load_cache=False
            publish_prediction=False
            with self.lock:
                if cache_signature!=self.cache_signature:
                    self.cache_signature=cache_signature
                    self.cache_changed_at=now
                    self.cache_pending=True
                if prediction_signature!=self.prediction_signature:
                    self.prediction_signature=prediction_signature
                    self.prediction_changed_at=now
                    self.prediction_pending=True
                if (
                    self.cache_pending
                    and now-self.cache_changed_at>=CACHE_QUIET_SECONDS
                ):
                    load_cache=True
                if (
                    self.prediction_pending
                    and now-self.prediction_changed_at
                    >=PREDICTION_QUIET_SECONDS
                ):
                    publish_prediction=True
            if load_cache:self.publish_cache()
            if publish_prediction:self.publish_prediction()

    def publish_cache(self):
        before=file_signature(self.cache_path)
        history=load_recent_history(
            self.cache_path,HISTORY_SCAN_SNAPSHOTS,HISTORY_POINTS
        )
        after=file_signature(self.cache_path)
        now=time.monotonic()
        with self.lock:
            if before!=after or after!=self.cache_signature:
                self.cache_signature=after
                self.cache_changed_at=now
                self.cache_pending=True
                return
            if after is None or not history.get("source_snapshots"):
                self.cache_changed_at=now
                self.cache_pending=True
                return
            encoded=json.dumps(
                history,separators=(",",":")
            ).encode("utf-8")
            self.history_payload=history
            self.history_encoded=encoded
            self.cache_pending=False

    def publish_prediction(self):
        signature=file_signature(self.prediction_path)
        now=time.monotonic()
        with self.lock:
            if signature!=self.prediction_signature:
                self.prediction_signature=signature
                self.prediction_changed_at=now
                self.prediction_pending=True
                return
            self.prediction_version=signature_version(signature)
            self.prediction_pending=False


def get_market_cache():
    global MARKET_CACHE
    with CONFIG_LOCK:
        if MARKET_CACHE is None:
            MARKET_CACHE=MarketCache(
                APP_DIR/"stocks_cache.jsonl",
                APP_DIR/"stocks_prediction.json"
            ).start()
        return(MARKET_CACHE)


def firewall_rule_exists():
    if os.name!="nt":return(True)
    result=subprocess.run(
        [
            "netsh","advfirewall","firewall","show","rule",
            f"name={FIREWALL_RULE}"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return(result.returncode==0)


def install_firewall_rule():
    if os.name!="nt":return(0)
    if not getattr(sys,"frozen",False):
        print("Build stocks_server.exe before installing its firewall rule.")
        return(1)
    executable=str(Path(sys.executable).resolve())
    subprocess.run(
        [
            "netsh","advfirewall","firewall","delete","rule",
            f"name={FIREWALL_RULE}"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    result=subprocess.run([
        "netsh","advfirewall","firewall","add","rule",
        f"name={FIREWALL_RULE}","dir=in","action=allow",
        "protocol=TCP",f"localport={PORT}",f"program={executable}",
        "profile=any","enable=yes"
    ])
    if result.returncode:
        print("Could not install the Windows Firewall rule.")
        return(1)
    print(f"Windows Firewall now allows TCP port {PORT} for {executable}.")
    return(0)


def ensure_firewall_rule():
    if (
        os.name!="nt"
        or not getattr(sys,"frozen",False)
        or firewall_rule_exists()
    ):
        return(True)
    print(f"Windows Firewall access is required for external TCP port {PORT}.")
    print("Approve the one-time administrator prompt to install it.")
    environment=os.environ.copy()
    environment["TORNX_SERVER_EXE"]=str(Path(sys.executable).resolve())
    command=(
        "$process=Start-Process -FilePath $env:TORNX_SERVER_EXE "
        "-ArgumentList '--install-firewall' -Verb RunAs -Wait -PassThru;"
        "exit $process.ExitCode"
    )
    result=subprocess.run(
        ["powershell.exe","-NoLogo","-NoProfile","-Command",command],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    if result.returncode or not firewall_rule_exists():
        print("Firewall access was not installed; LAN/public access may be blocked.")
        return(False)
    print("Windows Firewall access installed successfully.")
    return(True)


def local_addresses():
    addresses=set()
    try:
        for result in socket.getaddrinfo(
            socket.gethostname(),PORT,socket.AF_INET,socket.SOCK_STREAM
        ):
            address=result[4][0]
            if not address.startswith("127."):addresses.add(address)
    except OSError:
        pass
    return(sorted(addresses))


def main():
    if "--install-firewall" in sys.argv[1:]:
        return(install_firewall_rule())
    ensure_firewall_rule()
    market_cache=get_market_cache()
    try:
        server=Server((HOST,PORT),Handler)
    except OSError as error:
        market_cache.stop()
        print(f"Could not start TornX server on port {PORT}: {error}")
        return(1)
    print(f"TornX server listening on {HOST}:{PORT}")
    print(f"Open http://localhost:{PORT}")
    for address in local_addresses():
        print(f"Network: http://{address}:{PORT}")
    print("Direct prediction JSON access is disabled.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        print("\nTornX server stopped.")
    finally:
        server.server_close()
        market_cache.stop()
    return(0)
if __name__=="__main__":raise(SystemExit(main()))