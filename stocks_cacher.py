import argparse,json,os,shutil,subprocess,sys,time
from datetime import datetime,timedelta,timezone
from chalk import chalk,COLORS
from pathlib import Path
INTERVAL=15*60
REQUEST_DELAY=0.61
RATE_LIMIT_WAIT=60
LOOP_SAFETY_DELAY=3
ORANGE="\033[38;5;208m"
RESET="\033[0m"
API_URL="https://api.torn.com/v2/torn/stocks"
APP_DIR=Path(sys.executable).resolve().parent if getattr(sys,"frozen",False) else Path(__file__).resolve().parent
CACHE_FILE=APP_DIR/"stocks_cache.json"
BACKUP_FILE=CACHE_FILE.with_suffix(CACHE_FILE.suffix+".bak")
LIVE_CACHE_FILE=APP_DIR/"stocks_cache.jsonl"
LIVE_BACKUP_FILE=LIVE_CACHE_FILE.with_suffix(LIVE_CACHE_FILE.suffix+".bak")
FORWARD_FILE=APP_DIR/"stocks_forward_test.json"
API_KEY=os.environ.get("TORN_API_KEY","vJJeEEEZs9gxealK")
last_request_time=0.0
live_message=None
live_color=None

class RateLimitError(Exception):pass

def draw_live():
    if live_message is not None:
        width=max(20,shutil.get_terminal_size((120,20)).columns-1)
        message=live_message
        if len(message)>width:
            message=message[:width-3]+"..."
        print(
            f"\r\033[2K{chalk(message,color=live_color)}",
            end="",
            flush=True
        )

def set_live(message,color=COLORS.green):
    global live_message,live_color
    live_message=message
    live_color=color
    draw_live()

def finish_live():
    global live_message,live_color
    if live_message is not None:
        print()
        live_message=None
        live_color=None

def discard_live():
    global live_message,live_color
    print("\r\033[2K",end="",flush=True)
    live_message=None
    live_color=None

def debug(message,color=COLORS.cyan):
    if live_message is not None:
        print(f"\r\033[2K{chalk(message,color=color)}")
        draw_live()
    else:
        print(chalk(message,color=color))

def debug_ansi(message,color):
    if live_message is not None:
        print(f"\r\033[2K{color}{message}{RESET}")
        draw_live()
    else:
        print(f"{color}{message}{RESET}")

def aligned_timestamp(value):
    timestamp=int(value)
    return(timestamp//INTERVAL*INTERVAL)

def load_cache():
    if not CACHE_FILE.exists():return{}
    try:
        with CACHE_FILE.open("r",encoding="utf-8") as cache_file:
            cache=json.load(cache_file)
        return cache if isinstance(cache,dict) else {}
    except(OSError,json.JSONDecodeError) as error:
        debug(f"Could not read cache; starting fresh: {error}",COLORS.yellow)
        return{}

def load_frozen_cutoff():
    if not FORWARD_FILE.is_file():
        raise FileNotFoundError(
            f"frozen forward state not found: {FORWARD_FILE}"
        )
    with FORWARD_FILE.open("r",encoding="utf-8") as forward_file:
        state=json.load(forward_file)
    cutoff=int(state["cutoff_timestamp"])
    if cutoff%INTERVAL:
        raise ValueError("frozen cutoff is outside the 15-minute grid")
    return(cutoff)

def load_live_cache():
    if not LIVE_CACHE_FILE.exists():return{},False
    cache={}
    previous=None
    needs_sort=False
    with LIVE_CACHE_FILE.open("r",encoding="utf-8") as live_file:
        for line_number,line in enumerate(live_file,1):
            if not line.strip():continue
            record=json.loads(line)
            if not isinstance(record,dict) or len(record)!=1:
                raise ValueError(
                    f"invalid JSONL record on line {line_number}"
                )
            timestamp,stocks=next(iter(record.items()))
            value=int(timestamp)
            if value%INTERVAL or not isinstance(stocks,list):
                raise ValueError(
                    f"invalid JSONL snapshot on line {line_number}"
                )
            key=str(value)
            if key in cache:
                raise ValueError(
                    f"duplicate JSONL timestamp {value} on line {line_number}"
                )
            if previous is not None and value<=previous:
                needs_sort=True
            cache[key]=stocks
            previous=value
    return(cache,needs_sort)

def append_live_snapshot(timestamp,stocks):
    LIVE_CACHE_FILE.parent.mkdir(parents=True,exist_ok=True)
    with LIVE_CACHE_FILE.open("a",encoding="utf-8") as live_file:
        json.dump({str(timestamp):stocks},live_file,separators=(",",":"))
        live_file.write("\n")

def save_live_cache(cache):
    temporary_file=LIVE_CACHE_FILE.with_suffix(
        LIVE_CACHE_FILE.suffix+".tmp"
    )
    if LIVE_CACHE_FILE.exists():
        shutil.copy2(LIVE_CACHE_FILE,LIVE_BACKUP_FILE)
    with temporary_file.open("w",encoding="utf-8") as live_file:
        for timestamp,stocks in sorted(
            cache.items(),key=lambda item:int(item[0])
        ):
            json.dump({timestamp:stocks},live_file,separators=(",",":"))
            live_file.write("\n")
    temporary_file.replace(LIVE_CACHE_FILE)
    if LIVE_BACKUP_FILE.exists():
        LIVE_BACKUP_FILE.unlink()

def wait_for_request_limit():
    global last_request_time
    wait=REQUEST_DELAY-(time.monotonic()-last_request_time)
    if wait>0:time.sleep(wait)
    last_request_time=time.monotonic()

def error_details(payload):
    error=payload.get("error") if isinstance(payload,dict) else None
    if isinstance(error,dict):
        return error.get("code"),str(
            error.get("error") or error.get("message") or error
        )
    return None,str(error or "")

def rate_limit_countdown(message):
    for remaining in range(RATE_LIMIT_WAIT,0,-1):
        text=f"Rate limited ({message}). Retrying in {remaining}s..."
        print(f"\r\033[2K{chalk(text,color=COLORS.yellow)}",end="",flush=True)
        time.sleep(1)
    print(
        f"\r\033[2K"
        f"{chalk('Rate limit wait complete; retrying.',color=COLORS.yellow)}"
    )
    draw_live()

def fetch_stocks(timestamp):
    wait_for_request_limit()
    result=subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            f"{API_URL}?comment=Stocks%20Cacher&timestamp={timestamp}",
            "--header",
            "Accept: application/json",
            "--header",
            f"Authorization: ApiKey {API_KEY}",
            "--write-out",
            "\n%{http_code}"
        ],
        capture_output=True,
        text=True
    )
    if result.returncode:
        message=result.stderr.strip() or result.stdout.strip()
        raise OSError(message or f"curl failed with exit code {result.returncode}")
    body,status_text=result.stdout.rsplit("\n",1)
    status=int(status_text)
    if status==429:
        raise RateLimitError("HTTP 429")
    payload=json.loads(body)
    error_code,error_message=error_details(payload)
    message_lower=error_message.lower()
    if str(error_code)=="5" or "rate limit" in message_lower or "too many requests" in message_lower:
        raise RateLimitError(error_message or "API request limit reached")
    if status>=400:
        raise OSError(error_message or f"HTTP {status}")
    stocks=payload.get("stocks",[])
    if isinstance(stocks,dict):
        stocks=stocks.values()
    return[{
            "id":stock["acronym"],
            "price":stock["market"]["price"]
        }
        for stock in stocks
    ]

def save_cache(cache):
    temporary_file=CACHE_FILE.with_suffix(".tmp")
    if CACHE_FILE.exists():
        shutil.copy2(CACHE_FILE,BACKUP_FILE)
    with temporary_file.open("w",encoding="utf-8") as cache_file:
        json.dump(cache,cache_file,separators=(",",":"))
    temporary_file.replace(CACHE_FILE)
    if BACKUP_FILE.exists():
        BACKUP_FILE.unlink()

def progress_bar(current,total,label):
    ratio=current/total if total else 1
    width=30
    filled=int(width*ratio)
    bar="#"*filled+"-"*(width-filled)
    percentage=f"{ratio*100:.1f}".rstrip("0").rstrip(".")
    end="\n" if current>=total else ""
    print(
        f"\r\033[2K{label} [{bar}] {percentage}% ({current}/{total})",
        end=end,
        flush=True
    )

def sort_cache(cache,oldest_first):
    direction="oldest to newest" if oldest_first else "newest to oldest"
    items=sorted(
        cache.items(),
        key=lambda item:int(item[0]),
        reverse=not oldest_first
    )
    total=len(items)
    sorted_cache={}
    progress_bar(0,total,f"Sorting cache {direction}")
    for current,(timestamp,stocks) in enumerate(items,1):
        sorted_cache[timestamp]=stocks
        progress_bar(current,total,f"Sorting cache {direction}")
    return(sorted_cache)

def display_cached_range(oldest,newest,count,current,total):
    oldest_time=datetime.fromtimestamp(oldest).isoformat(sep=" ",timespec="seconds")
    if count==1:
        debug(
            f"{oldest_time} already exists in cache "
            f"({current} / {total} processed)",
            COLORS.green
        )
        return
    newest_time=datetime.fromtimestamp(newest).isoformat(sep=" ",timespec="seconds")
    debug_ansi(
        f"{oldest_time} to {newest_time} already exists in cache "
        f"({count} intervals)",
        ORANGE
    )

def display_success_range(oldest,newest,count,cached,current,total,stocks):
    oldest_time=datetime.fromtimestamp(oldest)
    newest_time=datetime.fromtimestamp(newest)
    if count==1:
        date_range=oldest_time.strftime("%Y-%m-%d %H:%M")
    elif oldest_time.date()==newest_time.date():
        date_range=(
            f"{oldest_time:%Y-%m-%d %H:%M} to "
            f"{newest_time:%H:%M}"
        )
    else:
        date_range=(
            f"{oldest_time:%Y-%m-%d %H:%M} to "
            f"{newest_time:%Y-%m-%d %H:%M}"
        )
    set_live(
        f"Cached {date_range} | {current}/{total} | new:{cached} | "
        f"range:{count} | stocks:{stocks}"
    )

def display_empty_range(oldest,newest,count,failed,current,total):
    oldest_time=datetime.fromtimestamp(oldest)
    newest_time=datetime.fromtimestamp(newest)
    if count==1:
        date_range=oldest_time.strftime("%Y-%m-%d %H:%M")
    elif oldest_time.date()==newest_time.date():
        date_range=(
            f"{oldest_time:%Y-%m-%d %H:%M}-"
            f"{newest_time:%H:%M}"
        )
    else:
        date_range=(
            f"{oldest_time:%Y-%m-%d %H:%M}-"
            f"{newest_time:%Y-%m-%d %H:%M}"
        )
    set_live(
        f"No stocks {date_range} | {current}/{total} | range:{count} | "
        f"failed:{failed} | skipped",
        COLORS.red
    )

def positive_days(value):
    days=int(value)
    if days<1:
        raise argparse.ArgumentTypeError("--days must be at least 1")
    return(days)

def parse_args():
    parser=argparse.ArgumentParser()
    parser.add_argument(
        "start",
        nargs="?",
        type=int,
        help="Unix timestamp used as the end of the cache range"
    )
    parser.add_argument(
        "--days",
        type=positive_days,
        default=1,
        help="calendar days to cache; --live starts at the frozen cutoff"
    )
    parser.add_argument(
        "--oldest-first",
        action="store_true",
        help="cache from the oldest timestamp to the newest"
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="sort the entire cache file by timestamp"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "write post-freeze snapshots to stocks_cache.jsonl in "
            "oldest-to-newest order"
        )
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "repeat --live at every 15-minute UTC window plus a "
            "3-second safety delay"
        )
    )
    args=parser.parse_args()
    if args.loop and not args.live:
        parser.error("--loop requires --live")
    if args.loop and args.start is not None:
        parser.error("--loop cannot be combined with a start timestamp")
    return(args)

def run_once(args,end_timestamp=None):
    end_time=aligned_timestamp(
        end_timestamp
        if end_timestamp is not None
        else args.start if args.start is not None else time.time()
    )
    end_day=datetime.fromtimestamp(end_time).replace(
        hour=0,minute=0,second=0,microsecond=0
    )
    first_day=end_day-timedelta(days=args.days-1)
    first_time=int(first_day.timestamp())
    try:
        if args.live:
            cutoff=load_frozen_cutoff()
            first_time=cutoff+INTERVAL
            cache,needs_sort=load_live_cache()
            if any(int(timestamp)<=cutoff for timestamp in cache):
                raise ValueError(
                    "live cache contains a timestamp at or before the cutoff"
                )
            if needs_sort:
                save_live_cache(cache)
            live_last=max(map(int,cache)) if cache else None
        else:
            cache=load_cache()
            live_last=None
    except(OSError,json.JSONDecodeError,KeyError,TypeError,ValueError) as error:
        debug(f"Could not initialize cache: {error}",COLORS.red)
        return(1)
    cached=0
    already_cached=0
    failed=0
    cached_range_newest=None
    cached_range_oldest=None
    cached_range_count=0
    success_range_newest=None
    success_range_oldest=None
    success_range_count=0
    success_latest_stocks=0
    empty_range_newest=None
    empty_range_oldest=None
    empty_range_count=0
    last_saved_cached=0
    live_dirty=False
    interrupted=False
    timestamps=(
        range(first_time,end_time+1,INTERVAL)
        if args.live or args.oldest_first
        else range(end_time,first_time-1,-INTERVAL)
    )
    total=len(timestamps)
    current=0
    try:
        for current,timestamp in enumerate(timestamps,1):
            cache_key=str(timestamp)
            if cache_key in cache:
                if empty_range_count:
                    finish_live()
                    empty_range_newest=None
                    empty_range_oldest=None
                    empty_range_count=0
                already_cached+=1
                if success_range_count:
                    display_success_range(
                        success_range_oldest,success_range_newest,
                        success_range_count,cached,current,total,
                        success_latest_stocks
                    )
                if cached_range_newest is None:
                    cached_range_newest=timestamp
                    cached_range_oldest=timestamp
                else:
                    cached_range_newest=max(cached_range_newest,timestamp)
                    cached_range_oldest=min(cached_range_oldest,timestamp)
                cached_range_count+=1
                continue
            if cached_range_count:
                display_cached_range(
                    cached_range_oldest,cached_range_newest,cached_range_count,
                    current-1,total
                )
                cached_range_newest=None
                cached_range_oldest=None
                cached_range_count=0
            try:
                while True:
                    try:
                        stocks=fetch_stocks(timestamp)
                        break
                    except RateLimitError as error:
                        rate_limit_countdown(str(error))
                if not stocks:
                    if success_range_count:
                        finish_live()
                        success_range_newest=None
                        success_range_oldest=None
                        success_range_count=0
                        success_latest_stocks=0
                    failed+=1
                    if empty_range_newest is None:
                        empty_range_newest=timestamp
                        empty_range_oldest=timestamp
                    else:
                        empty_range_newest=max(empty_range_newest,timestamp)
                        empty_range_oldest=min(empty_range_oldest,timestamp)
                    empty_range_count+=1
                    display_empty_range(
                        empty_range_oldest,empty_range_newest,
                        empty_range_count,failed,current,total
                    )
                    continue
                if empty_range_count:
                    finish_live()
                    empty_range_newest=None
                    empty_range_oldest=None
                    empty_range_count=0
                if (
                    args.live
                    and not live_dirty
                    and (live_last is None or timestamp>live_last)
                ):
                    append_live_snapshot(timestamp,stocks)
                    live_last=timestamp
                elif args.live:
                    live_dirty=True
                cache[cache_key]=stocks
                cached+=1
                if success_range_newest is None:
                    success_range_newest=timestamp
                    success_range_oldest=timestamp
                else:
                    success_range_newest=max(success_range_newest,timestamp)
                    success_range_oldest=min(success_range_oldest,timestamp)
                success_range_count+=1
                success_latest_stocks=len(stocks)
                display_success_range(
                    success_range_oldest,success_range_newest,
                    success_range_count,cached,current,total,
                    success_latest_stocks
                )
                if cached%1000==0:
                    if args.live:
                        if live_dirty:
                            save_live_cache(cache)
                            live_dirty=False
                            live_last=max(map(int,cache)) if cache else None
                        output_file=LIVE_CACHE_FILE
                    else:
                        save_cache(cache)
                        output_file=CACHE_FILE
                    last_saved_cached=cached
                    debug(
                        f"Checkpoint saved 1000 new snapshots to {output_file} "
                        f"({len(cache)} total snapshots).",
                        COLORS.yellow
                    )
            except(OSError,KeyError,TypeError,ValueError) as error:
                finish_live()
                empty_range_newest=None
                empty_range_oldest=None
                empty_range_count=0
                success_range_newest=None
                success_range_oldest=None
                success_range_count=0
                success_latest_stocks=0
                failed+=1
                debug(
                    f"Failed to cache stocks for {timestamp} "
                    f"({current} / {total}): {error}",
                    COLORS.red
                )
    except KeyboardInterrupt:
        interrupted=True
        discard_live()
    if cached_range_count:
        display_cached_range(
            cached_range_oldest,cached_range_newest,cached_range_count,
            current,total
        )
    finish_live()
    if args.live and live_dirty:
        save_live_cache(cache)
        live_dirty=False
        live_last=max(map(int,cache)) if cache else None
    if args.live and cached>last_saved_cached:
        debug(
            f"Finalized {cached-last_saved_cached} new snapshots in "
            f"{LIVE_CACHE_FILE} ({len(cache)} live snapshots)",
            COLORS.yellow
        )
        last_saved_cached=cached
    elif not args.live and cached>last_saved_cached:
        save_cache(cache)
        debug(
            f"Final save wrote {cached-last_saved_cached} new snapshots "
            f"to {CACHE_FILE} ({len(cache)} total snapshots)",
            COLORS.yellow
        )
        last_saved_cached=cached
    if args.sort:
        if args.live:
            save_live_cache(cache)
            debug(
                f"Saved live cache oldest to newest in {LIVE_CACHE_FILE}",
                COLORS.yellow
            )
        else:
            cache=sort_cache(cache,args.oldest_first)
            save_cache(cache)
            debug(f"Saved sorted cache to {CACHE_FILE}",COLORS.yellow)
    if interrupted:
        debug(
            f"Interrupted by user: {cached} cached, "
            f"{already_cached} already cached, {failed} failed.",
            COLORS.yellow
        )
        return(130)
    debug(
        f"Finished: {cached} cached, {already_cached} already cached, "
        f"{failed} failed.",
        COLORS.cyan
    )
    return(1 if failed else 0)

def next_loop_window(now=None):
    current=time.time() if now is None else float(now)
    window=aligned_timestamp(current)
    if current>window+LOOP_SAFETY_DELAY:
        window+=INTERVAL
    return(window)

def loop_countdown(window):
    target=window+LOOP_SAFETY_DELAY
    window_time=datetime.fromtimestamp(window,timezone.utc)
    while True:
        remaining=target-time.time()
        if remaining<=0:break
        seconds=max(1,int(remaining+.999))
        hours,remainder=divmod(seconds,3600)
        minutes,seconds=divmod(remainder,60)
        set_live(
            f"Next --live window {window_time:%Y-%m-%d %H:%M} UTC | "
            f"starts in {hours:02}:{minutes:02}:{seconds:02}",
            COLORS.cyan
        )
        time.sleep(min(1,remaining))
    set_live(
        f"Window {window_time:%Y-%m-%d %H:%M} UTC; "
        "running --live...",
        COLORS.green
    )
    finish_live()

def run_live_loop(args):
    window=next_loop_window()
    debug(
        "Live loop enabled: caching every 15-minute UTC window after",
        COLORS.cyan
    )
    while True:
        loop_countdown(window)
        result=run_once(args,window)
        if result==130:return(130)
        window+=INTERVAL
        latest_due=aligned_timestamp(time.time()-LOOP_SAFETY_DELAY)
        if latest_due>window:
            window=latest_due

def main():
    args=parse_args()
    if not args.loop:return(run_once(args))
    try:return(run_live_loop(args))
    except KeyboardInterrupt:
        discard_live()
        debug("Live loop interrupted by user; exiting.",COLORS.yellow)
        return(130)
if __name__=="__main__":raise SystemExit(main())