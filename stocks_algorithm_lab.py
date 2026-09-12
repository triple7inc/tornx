import argparse,hashlib,json,mmap,re,sys,uuid
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
except ImportError as error:
    raise SystemExit("NumPy is required: python -m pip install numpy") from error


INTERVAL=15*60
BOUNDARY=re.compile(rb'"(\d{10})":\[')
APP_DIR=(
    Path(sys.executable).resolve().parent
    if getattr(sys,"frozen",False)
    else Path(__file__).resolve().parent
)
DEFAULT_CACHE=APP_DIR/"stocks_cache.json"
DEFAULT_LIVE_CACHE=APP_DIR/"stocks_cache.jsonl"
DEFAULT_FORWARD=APP_DIR/"stocks_forward_test.json"
MODEL_NAMES=("auto","persistence","previous","ar2","extrapolation")
THRESHOLDS=(0.50,0.75,0.90,0.95,0.99)
EVENT_CELL_THRESHOLDS=(0,5,8,10,12,15,20,25,30,40,50)
EVENT_CHAIN_THRESHOLDS=(0,.10,.25,.50,.75,.90)
EVENT_COUNT_THRESHOLDS=(0,5,10,20,25,30)
EVENT_STRONG_Z=10
EVENT_SAFETY_MARGIN=.015
EVENT_MIN_ACTIONS=100
CONDITIONAL_CELL_Z=15
PRICE_RUN_CANDIDATES=(1,2,3,5,10,20,50,99)
PRICE_HORIZON_CANDIDATES=(1,2,3,5,10,20,30,60,99)
PRICE_MIN_ACTIONS=500
DEFAULT_SELL_FEE=.001
FORWARD_VERSION=1
FORWARD_SCHEMA="torn-stock-forward-v1"
FORWARD_MIN_DAYS=30
FORWARD_MIN_CLUSTERS=35
FORWARD_MIN_CALENDAR_DAYS=90
FORWARD_CLUSTER_GAP=24*60*60


def validation_fraction(value):
    value=float(value)
    if not 0.05<=value<=0.50:
        raise argparse.ArgumentTypeError("validation must be between 0.05 and 0.50")
    return(value)


def positive_integer(value):
    value=int(value)
    if value<1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return(value)


def target_accuracy(value):
    value=float(value)
    if not 50<=value<100:
        raise argparse.ArgumentTypeError("accuracy must be at least 50 and below 100")
    return(value/100)


def fee_percent(value):
    value=float(value)
    if not 0<=value<100:
        raise argparse.ArgumentTypeError("fee must be at least 0 and below 100")
    return(value/100)


def parse_args():
    parser=argparse.ArgumentParser(
        description=(
            "Reverse-engineer the cached 15-minute stock process, backtest "
            "candidate algorithms, and predict the next window."
        )
    )
    parser.add_argument("--cache",type=Path,default=DEFAULT_CACHE)
    parser.add_argument(
        "--live-cache",
        type=Path,
        default=DEFAULT_LIVE_CACHE,
        help="append-only JSONL snapshots merged after the base cache"
    )
    parser.add_argument(
        "--forward-file",
        type=Path,
        default=DEFAULT_FORWARD,
        help="frozen forward-test state (default: beside this program)"
    )
    parser.add_argument("--validation",type=validation_fraction,default=.20)
    parser.add_argument("--model",choices=MODEL_NAMES,default="auto")
    parser.add_argument("--top",type=positive_integer,default=35)
    parser.add_argument(
        "--target-accuracy",
        type=target_accuracy,
        default=.90,
        metavar="PERCENT",
        help="minimum honest high-confidence action accuracy (default: 90)"
    )
    parser.add_argument(
        "--sell-fee",
        type=fee_percent,
        default=DEFAULT_SELL_FEE,
        metavar="PERCENT",
        help="sale fee used by the trade backtest (default: 0.1)"
    )
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="predict and score only the next distinct changed state"
    )
    parser.add_argument(
        "--event-samples",
        type=positive_integer,
        default=10,
        help="number of held-out changed-window examples to display"
    )
    forward=parser.add_mutually_exclusive_group()
    forward.add_argument(
        "--freeze-forward",
        action="store_true",
        help="freeze the current model and cache cutoff for future testing"
    )
    forward.add_argument(
        "--forward-test",
        action="store_true",
        help="score only post-cutoff events with the frozen model"
    )
    parser.add_argument("--json",action="store_true")
    return(parser.parse_args())


def decode_prices(raw):
    stocks=json.loads(raw)
    ids=[stock.get("id") or stock.get("acronym") for stock in stocks]
    prices=[float(stock["price"]) for stock in stocks]
    return(ids,prices)


def load_runs(cache_file,live_cache_file=None,base_cutoff=None):
    if not cache_file.is_file():
        raise FileNotFoundError(cache_file)
    if cache_file.stat().st_size==0:
        raise ValueError("cache file is empty")

    vectors=[]
    lengths=[]
    timestamp_count=0
    timestamps=[]
    minimum_timestamp=None
    maximum_timestamp=None
    stock_ids=None

    with cache_file.open("rb") as file,mmap.mmap(
        file.fileno(),0,access=mmap.ACCESS_READ
    ) as mapped:
        matches=BOUNDARY.finditer(mapped)
        try:
            first=next(matches)
        except StopIteration as error:
            raise ValueError("cache contains no timestamped snapshots") from error

        first_timestamp=int(first.group(1))
        timestamps.append(first_timestamp)
        previous_timestamp=first_timestamp
        minimum_timestamp=first_timestamp
        maximum_timestamp=first_timestamp
        timestamp_count=1
        value_start=first.end()-1
        previous_value=None
        run_length=1

        for match in matches:
            timestamp=int(match.group(1))
            timestamps.append(timestamp)
            timestamp_count+=1
            minimum_timestamp=min(minimum_timestamp,timestamp)
            maximum_timestamp=max(maximum_timestamp,timestamp)
            value=mapped[value_start:match.start()-1]
            if previous_value is None:
                previous_value=value
                stock_ids,prices=decode_prices(value)
                vectors.append(prices)
            elif value==previous_value:
                run_length+=1
            else:
                lengths.append(run_length)
                ids,prices=decode_prices(value)
                if ids!=stock_ids:
                    raise ValueError("stock order or membership changes inside cache")
                vectors.append(prices)
                previous_value=value
                run_length=1
            previous_timestamp=timestamp
            value_start=match.end()-1

        value=mapped[value_start:len(mapped)-1]
        if previous_value is None:
            stock_ids,prices=decode_prices(value)
            vectors.append(prices)
        elif value==previous_value:
            run_length+=1
        else:
            lengths.append(run_length)
            ids,prices=decode_prices(value)
            if ids!=stock_ids:
                raise ValueError("stock order or membership changes inside cache")
            vectors.append(prices)
            run_length=1
        lengths.append(run_length)

    states=np.asarray(vectors,dtype=np.float64)
    run_lengths=np.asarray(lengths,dtype=np.int64)
    timestamps=np.asarray(timestamps,dtype=np.int64)
    state_starts=[]
    state_ends=[]
    offset=0
    for length in run_lengths:
        values=timestamps[offset:offset+length]
        state_starts.append(int(values.min()))
        state_ends.append(int(values.max()))
        offset+=int(length)
    state_starts=np.asarray(state_starts,dtype=np.int64)
    state_ends=np.asarray(state_ends,dtype=np.int64)
    if first_timestamp>previous_timestamp:
        states=states[::-1].copy()
        run_lengths=run_lengths[::-1].copy()
        state_starts=state_starts[::-1].copy()
        state_ends=state_ends[::-1].copy()
        timestamps=timestamps[::-1].copy()
    differences=np.diff(timestamps)
    if np.any(differences<=0):
        raise ValueError("cache contains duplicate or unordered timestamps")
    if np.any(timestamps%INTERVAL):
        raise ValueError("cache contains timestamps outside the 15-minute grid")
    if len(states)<100:
        raise ValueError("at least 100 distinct consecutive states are required")
    if int(run_lengths.sum())!=timestamp_count:
        raise ValueError("run lengths do not cover every timestamp")
    data={
        "stock_ids":stock_ids,
        "states":states,
        "run_lengths":run_lengths,
        "state_starts":state_starts,
        "state_ends":state_ends,
        "timestamps":timestamps,
        "timestamp_count":timestamp_count,
        "minimum_timestamp":minimum_timestamp,
        "maximum_timestamp":maximum_timestamp
    }
    if base_cutoff is not None:
        data=truncate_runs(data,base_cutoff)
    return(merge_live_cache(data,live_cache_file))


def fit_ar2(states,split):
    indexes=np.arange(2,split-1)
    coefficients=np.empty((states.shape[1],3),dtype=np.float64)
    for column in range(states.shape[1]):
        design=np.column_stack((
            np.ones(len(indexes)),
            states[indexes,column],
            states[indexes-1,column]
        ))
        coefficients[column]=np.linalg.lstsq(
            design,states[indexes+1,column],rcond=None
        )[0]
    return(coefficients)


def transition_predictions(states,indexes,model,ar2_coefficients=None):
    if model=="persistence":
        return(states[indexes].copy())
    if model=="previous":
        return(states[indexes-1].copy())
    if model=="extrapolation":
        return(2*states[indexes]-states[indexes-1])
    if model=="ar2":
        result=np.empty((len(indexes),states.shape[1]),dtype=np.float64)
        for column in range(states.shape[1]):
            coefficient=ar2_coefficients[column]
            result[:,column]=(
                coefficient[0]
                +coefficient[1]*states[indexes,column]
                +coefficient[2]*states[indexes-1,column]
            )
        return(result)
    raise ValueError(f"unknown model: {model}")


def empirical_hazards(run_lengths,split):
    completed=run_lengths[:split]
    maximum=int(completed.max())
    hazards=np.zeros(maximum+2,dtype=np.float64)
    for age in range(1,maximum+1):
        survived=np.sum(completed>=age)
        if survived:
            hazards[age]=np.sum(completed==age)/survived
    hazards[maximum+1]=1
    return(hazards)


def hazard_at(hazards,age):
    return(float(hazards[age]) if age<len(hazards) else 1.0)


def empty_metrics(name,model,threshold):
    return({
        "name":name,
        "model":model,
        "threshold":threshold,
        "windows":0,
        "vector_correct":0,
        "price_correct":0,
        "price_total":0,
        "absolute_error":0.0,
        "changed_prices":0,
        "changed_price_correct":0,
        "direction_correct":0,
        "move_state_correct":0
    })


def add_score(metrics,predicted,actual,current,weight=1):
    predicted=np.round(predicted,2)
    exact=predicted==actual
    moved=actual!=current
    predicted_move=predicted!=current
    metrics["windows"]+=weight
    metrics["vector_correct"]+=int(np.all(exact))*weight
    metrics["price_correct"]+=int(np.sum(exact))*weight
    metrics["price_total"]+=len(actual)*weight
    metrics["absolute_error"]+=float(np.sum(np.abs(predicted-actual)))*weight
    metrics["move_state_correct"]+=int(
        bool(np.any(predicted_move))==bool(np.any(moved))
    )*weight
    if np.any(moved):
        metrics["changed_prices"]+=int(np.sum(moved))*weight
        metrics["changed_price_correct"]+=int(np.sum(exact&moved))*weight
        metrics["direction_correct"]+=int(np.sum(
            (np.sign(predicted-current)==np.sign(actual-current))&moved
        ))*weight


def finalize_metrics(metrics):
    windows=max(1,metrics["windows"])
    price_total=max(1,metrics["price_total"])
    changed=max(1,metrics["changed_prices"])
    return({
        "name":metrics["name"],
        "model":metrics["model"],
        "threshold":metrics["threshold"],
        "windows":metrics["windows"],
        "vector_accuracy_percent":metrics["vector_correct"]/windows*100,
        "price_accuracy_percent":metrics["price_correct"]/price_total*100,
        "changed_price_accuracy_percent":metrics["changed_price_correct"]/changed*100,
        "changed_direction_accuracy_percent":metrics["direction_correct"]/changed*100,
        "move_state_accuracy_percent":metrics["move_state_correct"]/windows*100,
        "mae":metrics["absolute_error"]/price_total
    })


def backtest_strategy(
    states,run_lengths,split,hazards,model,threshold,ar2_coefficients
):
    name=(
        "persistence"
        if model=="persistence"
        else f"{model}@hazard>={threshold:.2f}"
    )
    metrics=empty_metrics(name,model,threshold)
    indexes=np.arange(split,len(states)-1)
    transition=transition_predictions(
        states,indexes,model,ar2_coefficients
    )
    for position,index in enumerate(indexes):
        current=states[index]
        next_state=states[index+1]
        length=int(run_lengths[index])
        if model=="persistence":
            add_score(metrics,current,current,current,max(0,length-1))
            add_score(metrics,current,next_state,current)
            continue
        transition_before=sum(
            hazard_at(hazards,age)>=threshold
            for age in range(1,length)
        )
        persistent_before=length-1-transition_before
        if persistent_before:
            add_score(metrics,current,current,current,persistent_before)
        if transition_before:
            add_score(
                metrics,transition[position],current,current,transition_before
            )
        predicted=(
            transition[position]
            if hazard_at(hazards,length)>=threshold
            else current
        )
        add_score(metrics,predicted,next_state,current)
    return(finalize_metrics(metrics))


def transition_backtests(states,split,ar2_coefficients):
    indexes=np.arange(split,len(states)-1)
    results=[]
    for model in ("persistence","previous","ar2","extrapolation"):
        prediction=transition_predictions(
            states,indexes,model,ar2_coefficients
        )
        metrics=empty_metrics(model,model,None)
        for position,index in enumerate(indexes):
            add_score(
                metrics,prediction[position],states[index+1],states[index]
            )
        results.append(finalize_metrics(metrics))
    return(results)


def event_examples(data,split,model,ar2_coefficients,count):
    states=data["states"]
    indexes=np.arange(split,len(states)-1)
    prediction=np.round(transition_predictions(
        states,indexes,model,ar2_coefficients
    ),2)
    examples=[]
    for position,index in list(enumerate(indexes))[-count:]:
        current=states[index]
        actual=states[index+1]
        predicted=prediction[position]
        moved=actual!=current
        exact=predicted==actual
        examples.append({
            "timestamp":int(data["state_starts"][index+1]),
            "time":datetime.fromtimestamp(
                int(data["state_starts"][index+1])
            ).isoformat(sep=" ",timespec="minutes"),
            "exact_vector":bool(np.all(exact)),
            "exact_prices":int(np.sum(exact)),
            "changed_prices":int(np.sum(moved)),
            "exact_changed_prices":int(np.sum(exact&moved)),
            "correct_changed_directions":int(np.sum(
                (np.sign(predicted-current)==np.sign(actual-current))&moved
            )),
            "mae":float(np.mean(np.abs(predicted-actual)))
        })
    return(examples)


def choose_models(states,run_lengths,validation,requested_model):
    split=max(20,min(len(states)-20,int(len(states)*(1-validation))))
    ar2_coefficients=fit_ar2(states,split)
    hazards=empirical_hazards(run_lengths,split)
    candidates=[backtest_strategy(
        states,run_lengths,split,hazards,"persistence",1.01,ar2_coefficients
    )]
    allowed=("previous","ar2","extrapolation")
    for model in allowed:
        for threshold in THRESHOLDS:
            candidates.append(backtest_strategy(
                states,run_lengths,split,hazards,model,threshold,
                ar2_coefficients
            ))
    candidates.sort(
        key=lambda result:(
            result["vector_accuracy_percent"],
            result["price_accuracy_percent"],
            -result["mae"]
        ),
        reverse=True
    )
    if requested_model=="auto":
        selected=candidates[0]
    elif requested_model=="persistence":
        selected=next(result for result in candidates if result["model"]=="persistence")
    else:
        matching=[result for result in candidates if result["model"]==requested_model]
        selected=max(
            matching,
            key=lambda result:(
                result["vector_accuracy_percent"],
                result["price_accuracy_percent"],
                -result["mae"]
            )
        )
    transitions=transition_backtests(states,split,ar2_coefficients)
    transitions.sort(
        key=lambda result:(result["mae"],-result["price_accuracy_percent"])
    )
    return(split,hazards,ar2_coefficients,candidates,transitions,selected)


def infer_structure(data):
    states=data["states"]
    lengths=data["run_lengths"]
    changes=np.diff(states,axis=0)
    changed_counts=np.sum(changes!=0,axis=1)
    years=(
        data["maximum_timestamp"]-data["minimum_timestamp"]
    )/(365.2425*86400)
    bands=(states.max(axis=0)/states.min(axis=0)-1)*100
    return({
        "years":years,
        "windows":data["timestamp_count"],
        "stocks":states.shape[1],
        "consecutive_states":len(states),
        "changing_window_percent":(len(states)-1)/max(1,data["timestamp_count"]-1)*100,
        "median_hold_hours":float(np.median(lengths)*.25),
        "dominant_hold_windows":int(np.bincount(lengths).argmax()),
        "median_stocks_changed":float(np.median(changed_counts)),
        "median_price_band_percent":float(np.median(bands)),
        "minimum_price_band_percent":float(np.min(bands)),
        "maximum_price_band_percent":float(np.max(bands))
    })


def cents(values):
    return(np.rint(np.asarray(values,dtype=np.float64)*100).astype(np.int64))


def truncate_runs(data,cutoff):
    timestamps=data["timestamps"]
    count=int(np.searchsorted(timestamps,int(cutoff),side="right"))
    if count<1:
        raise ValueError("frozen cutoff precedes the base cache")
    if count==len(timestamps):return(data)
    cumulative=np.cumsum(data["run_lengths"])
    run_count=int(np.searchsorted(cumulative,count,side="left"))+1
    run_lengths=data["run_lengths"][:run_count].copy()
    run_lengths[-1]=count-int(np.sum(run_lengths[:-1]))
    data["states"]=data["states"][:run_count].copy()
    data["run_lengths"]=run_lengths
    data["state_starts"]=data["state_starts"][:run_count].copy()
    data["state_ends"]=data["state_ends"][:run_count].copy()
    data["state_ends"][-1]=timestamps[count-1]
    data["timestamps"]=timestamps[:count].copy()
    data["timestamp_count"]=count
    data["maximum_timestamp"]=int(timestamps[count-1])
    return(data)


def merge_live_cache(data,live_cache_file):
    if live_cache_file is None or not live_cache_file.is_file():
        return(data)
    records=[]
    previous=None
    with live_cache_file.open("r",encoding="utf-8") as live_file:
        for line_number,line in enumerate(live_file,1):
            if not line.strip():continue
            record=json.loads(line)
            if not isinstance(record,dict) or len(record)!=1:
                raise ValueError(
                    f"invalid live-cache record on line {line_number}"
                )
            timestamp,stocks=next(iter(record.items()))
            timestamp=int(timestamp)
            if timestamp%INTERVAL or not isinstance(stocks,list) or not stocks:
                raise ValueError(
                    f"invalid live-cache snapshot on line {line_number}"
                )
            if previous is not None and timestamp<=previous:
                raise ValueError(
                    "live-cache timestamps must be strictly oldest to newest"
                )
            ids=[stock.get("id") or stock.get("acronym") for stock in stocks]
            if ids!=data["stock_ids"]:
                raise ValueError(
                    f"live-cache stock order changed on line {line_number}"
                )
            prices=cents([float(stock["price"]) for stock in stocks])
            records.append((timestamp,prices))
            previous=timestamp
    if not records:return(data)
    base_timestamps=data["timestamps"]
    cumulative=np.cumsum(data["run_lengths"])
    base_prices=cents(data["states"])
    additions=[]
    for timestamp,prices in records:
        position=int(np.searchsorted(base_timestamps,timestamp))
        if (
            position<len(base_timestamps)
            and int(base_timestamps[position])==timestamp
        ):
            state_index=int(np.searchsorted(
                cumulative,position,side="right"
            ))
            if not np.array_equal(base_prices[state_index],prices):
                raise ValueError(
                    f"live-cache conflicts with base timestamp {timestamp}"
                )
            continue
        if timestamp<=int(base_timestamps[-1]):
            raise ValueError(
                f"live-cache timestamp {timestamp} falls inside a base gap"
            )
        additions.append((timestamp,prices))
    if not additions:return(data)
    extension=0
    added_states=[]
    added_lengths=[]
    added_starts=[]
    added_ends=[]
    current=base_prices[-1]
    for timestamp,prices in additions:
        if np.array_equal(prices,current):
            if added_lengths:
                added_lengths[-1]+=1
                added_ends[-1]=timestamp
            else:
                extension+=1
            continue
        added_states.append(prices.astype(np.float64)/100)
        added_lengths.append(1)
        added_starts.append(timestamp)
        added_ends.append(timestamp)
        current=prices
    timestamps=np.concatenate((
        base_timestamps,
        np.asarray([item[0] for item in additions],dtype=np.int64)
    ))
    run_lengths=data["run_lengths"].copy()
    state_ends=data["state_ends"].copy()
    if extension:
        run_lengths[-1]+=extension
        state_ends[-1]=additions[extension-1][0]
    if added_states:
        data["states"]=np.vstack((
            data["states"],np.asarray(added_states,dtype=np.float64)
        ))
        run_lengths=np.concatenate((
            run_lengths,np.asarray(added_lengths,dtype=np.int64)
        ))
        data["state_starts"]=np.concatenate((
            data["state_starts"],np.asarray(added_starts,dtype=np.int64)
        ))
        state_ends=np.concatenate((
            state_ends,np.asarray(added_ends,dtype=np.int64)
        ))
    data["run_lengths"]=run_lengths
    data["state_ends"]=state_ends
    data["timestamps"]=timestamps
    data["timestamp_count"]=len(timestamps)
    data["maximum_timestamp"]=int(timestamps[-1])
    return(data)


def wilson_lower(correct,total):
    if total<=0:
        return(0.0)
    probability=correct/total
    z=1.96
    denominator=1+z*z/total
    center=probability+z*z/(2*total)
    margin=z*np.sqrt(
        probability*(1-probability)/total+z*z/(4*total*total)
    )
    return(float((center-margin)/denominator))


def event_scale(changes,end):
    scale=np.median(np.abs(changes[:end]),axis=0).astype(np.float64)
    scale[scale<1]=1
    return(scale)


def prepare_event_block(
    states,changes,indexes,scale,timestamps,strong_z=EVENT_STRONG_Z
):
    previous=changes[indexes-1]
    earlier=changes[indexes-2]
    actual=changes[indexes]
    previous_moved=previous!=0
    actual_moved=actual!=0
    strength=np.abs(previous)/scale
    previous_count=np.maximum(1,np.sum(previous_moved,axis=1))
    chain_fraction=np.sum(
        (previous==-earlier)&previous_moved,axis=1
    )/previous_count
    predicted=states[indexes]-previous
    exact=predicted==states[indexes+1]
    return({
        "indexes":indexes,
        "timestamps":timestamps[indexes+1],
        "current":states[indexes],
        "previous":previous,
        "actual":actual,
        "previous_moved":previous_moved,
        "actual_moved":actual_moved,
        "strength":strength,
        "chain_fraction":chain_fraction,
        "strong_count":np.sum(strength>=strong_z,axis=1),
        "correct_direction":actual_moved&(
            np.sign(actual)==-np.sign(previous)
        ),
        "exact":exact,
        "vector_exact":np.all(exact,axis=1)
    })


def event_rule_masks(block,rule):
    active=(
        (block["chain_fraction"]>=rule["chain_fraction"])
        &(block["strong_count"]>=rule["strong_count"])
    )
    issued=(
        block["previous_moved"]
        &(block["strength"]>=rule["cell_z"])
        &active[:,None]
    )
    return(active,issued)


def dependence_counts(block,active):
    indexes=block["indexes"][active]
    clusters=(
        0 if not len(indexes)
        else 1+int(np.sum(np.diff(indexes)>1))
    )
    days=len(np.unique(block["timestamps"][active]//86400))
    return(days,clusters)


def score_event_rule(block,rule):
    active,issued=event_rule_masks(block,rule)
    issued_count=int(np.sum(issued))
    correct=int(np.sum(issued&block["correct_direction"]))
    issued_moved=issued&block["actual_moved"]
    moved_count=int(np.sum(issued_moved))
    exact=int(np.sum(issued&block["exact"]))
    changed_total=int(np.sum(block["actual_moved"]))
    active_count=int(np.sum(active))
    vector_exact=int(np.sum(active&block["vector_exact"]))
    predicted_direction=-np.sign(block["previous"])
    gross_return=predicted_direction*block["actual"]/block["current"]
    positive_events=0
    for row in np.flatnonzero(active):
        if np.any(issued[row]) and np.mean(gross_return[row][issued[row]])>0:
            positive_events+=1
    days,clusters=dependence_counts(block,active)
    return({
        "events":len(block["indexes"]),
        "active_events":active_count,
        "active_event_percent":active_count/max(1,len(block["indexes"]))*100,
        "issued_actions":issued_count,
        "distinct_days":days,
        "event_clusters":clusters,
        "positive_events":positive_events,
        "positive_event_percent":positive_events/max(1,active_count)*100,
        "positive_event_wilson_lower_percent":wilson_lower(
            positive_events,active_count
        )*100,
        "correct_actions":correct,
        "action_accuracy_percent":correct/max(1,issued_count)*100,
        "action_wilson_lower_percent":wilson_lower(
            correct,issued_count
        )*100,
        "participation_percent":moved_count/max(1,issued_count)*100,
        "changed_direction_percent":correct/max(1,moved_count)*100,
        "changed_cell_coverage_percent":moved_count/max(1,changed_total)*100,
        "exact_price_percent":exact/max(1,issued_count)*100,
        "exact_vector_percent":vector_exact/max(1,active_count)*100
    })


def score_precision_trade(
    block,rule,prior_prior_runs,max_prior_run,sell_fee
):
    active,issued=event_rule_masks(block,rule)
    active=active&(prior_prior_runs<=max_prior_run)
    predicted_move=np.abs(block["previous"])/block["current"]
    issued=(
        issued&active[:,None]
        &(predicted_move>=sell_fee)
    )
    issued_count=int(np.sum(issued))
    exact_count=int(np.sum(issued&block["exact"]))
    predicted_direction=-np.sign(block["previous"])
    actual_return=block["actual"]/block["current"]
    net_return=np.where(
        predicted_direction>0,
        (1+actual_return)*(1-sell_fee)-1,
        (1-sell_fee)/(1+actual_return)-1
    )
    values=net_return[issued]
    profitable=int(np.sum(values>0))
    event_returns=[]
    for row in np.flatnonzero(active):
        if np.any(issued[row]):
            event_returns.append(float(np.mean(net_return[row][issued[row]])))
    event_returns=np.asarray(event_returns,dtype=np.float64)
    positive_events=int(np.sum(event_returns>0))
    if len(event_returns):
        wealth=np.cumprod(1+event_returns)
        peak=np.maximum.accumulate(wealth)
        compound=float((wealth[-1]-1)*100)
        drawdown=float(np.min(wealth/peak-1)*100)
    else:
        compound=0.0
        drawdown=0.0
    active_with_actions=np.any(issued,axis=1)
    days,clusters=dependence_counts(block,active_with_actions)
    vector_exact=int(np.sum(active_with_actions&block["vector_exact"]))
    issued_vector_exact=int(np.sum(
        active_with_actions&np.all((~issued)|block["exact"],axis=1)
    ))
    active_count=int(np.sum(active_with_actions))

    def side_metrics(side_mask):
        count=int(np.sum(side_mask))
        exact=int(np.sum(side_mask&block["exact"]))
        side_values=net_return[side_mask]
        profitable=int(np.sum(side_values>0))
        return({
            "actions":count,
            "exact_prices":exact,
            "exact_price_percent":exact/max(1,count)*100,
            "exact_price_wilson_lower_percent":wilson_lower(
                exact,count
            )*100,
            "profitable_actions":profitable,
            "profitable_action_percent":profitable/max(1,count)*100,
            "mean_net_bps":float(np.mean(side_values)*10000)
            if count else 0.0
        })

    return({
        "maximum_prior_run":int(max_prior_run),
        "fee_model":"sell_only_once",
        "sell_fee_percent":sell_fee*100,
        "minimum_predicted_move_bps":sell_fee*10000,
        "events":active_count,
        "distinct_days":days,
        "event_clusters":clusters,
        "issued_actions":issued_count,
        "exact_prices":exact_count,
        "exact_price_percent":exact_count/max(1,issued_count)*100,
        "exact_price_wilson_lower_percent":wilson_lower(
            exact_count,issued_count
        )*100,
        "full_reversal_exact_vectors":vector_exact,
        "full_reversal_exact_vector_percent":vector_exact/max(
            1,active_count
        )*100,
        "full_reversal_vector_wilson_lower_percent":wilson_lower(
            vector_exact,active_count
        )*100,
        "issued_exact_vectors":issued_vector_exact,
        "issued_exact_vector_percent":issued_vector_exact/max(
            1,active_count
        )*100,
        "profitable_actions":profitable,
        "profitable_action_percent":profitable/max(1,issued_count)*100,
        "profitable_action_wilson_lower_percent":wilson_lower(
            profitable,issued_count
        )*100,
        "mean_net_bps":float(np.mean(values)*10000) if len(values) else 0.0,
        "median_net_bps":float(np.median(values)*10000) if len(values) else 0.0,
        "positive_events":positive_events,
        "positive_event_percent":positive_events/max(1,active_count)*100,
        "positive_event_wilson_lower_percent":wilson_lower(
            positive_events,active_count
        )*100,
        "event_compound_percent":compound,
        "event_max_drawdown_percent":drawdown,
        "buy":side_metrics(issued&(predicted_direction>0)),
        "sell_or_avoid":side_metrics(issued&(predicted_direction<0))
    })


def score_trade_horizon(
    block,rule,prior_prior_runs,max_prior_run,current_runs,sell_fee,horizon
):
    active,issued=event_rule_masks(block,rule)
    active=active&(prior_prior_runs<=max_prior_run)
    issued=(
        issued&active[:,None]
        &(np.abs(block["previous"])/block["current"]>=sell_fee)
    )
    active=np.any(issued,axis=1)
    events=int(np.sum(active))
    within=int(np.sum(active&(current_runs<=horizon)))
    return({
        "horizon_windows":int(horizon),
        "events":events,
        "within_horizon":within,
        "within_horizon_percent":within/max(1,events)*100,
        "within_horizon_wilson_lower_percent":wilson_lower(
            within,events
        )*100,
        "maximum_observed_windows":int(np.max(current_runs[active]))
        if events else 0
    })


def event_rule_candidates():
    for cell_z in EVENT_CELL_THRESHOLDS:
        for chain_fraction in EVENT_CHAIN_THRESHOLDS:
            for strong_count in EVENT_COUNT_THRESHOLDS:
                yield({
                    "cell_z":cell_z,
                    "chain_fraction":chain_fraction,
                    "strong_count":strong_count
                })


def select_event_rule(
    states,changes,target,timestamps,run_lengths,sell_fee
):
    count=len(changes)
    boundaries=(
        int(count*.40),int(count*.55),int(count*.70),int(count*.80)
    )
    folds=[]
    for start,end in zip(boundaries,boundaries[1:]):
        start=max(2,start)
        indexes=np.arange(start,end)
        folds.append(prepare_event_block(
            states,changes,indexes,event_scale(changes,start),timestamps
        ))
    required=min(.99,target+EVENT_SAFETY_MARGIN)*100
    passing=[]
    fallback=[]
    for rule in event_rule_candidates():
        scores=[score_event_rule(block,rule) for block in folds]
        minimum_lower=min(
            score["action_wilson_lower_percent"] for score in scores
        )
        total=sum(score["issued_actions"] for score in scores)
        key=(
            total,rule["strong_count"],rule["chain_fraction"],
            -rule["cell_z"]
        )
        fallback.append((minimum_lower,total,key,rule,scores))
        if all(
            score["issued_actions"]>=EVENT_MIN_ACTIONS
            and score["action_wilson_lower_percent"]>=required
            for score in scores
        ):
            passing.append((key,rule,scores))
    if passing:
        _,rule,fold_scores=max(passing,key=lambda item:item[0])
        calibration_passed=True
    else:
        _,_,_,rule,fold_scores=max(
            fallback,key=lambda item:(item[0],item[1])
        )
        calibration_passed=False

    test_start=boundaries[-1]
    test_block=prepare_event_block(
        states,changes,np.arange(test_start,count),
        event_scale(changes,test_start),timestamps
    )
    test=score_event_rule(test_block,rule)
    conditional_rule={
        "cell_z":CONDITIONAL_CELL_Z,
        "chain_fraction":0,
        "strong_count":0
    }
    conditional_test=score_event_rule(test_block,conditional_rule)

    price_start=int(count*.60)
    price_indexes=np.arange(price_start,test_start)
    price_block=prepare_event_block(
        states,changes,price_indexes,event_scale(changes,price_start),
        timestamps
    )
    price_candidates=[]
    price_fallback=[]
    for maximum in PRICE_RUN_CANDIDATES:
        score=score_precision_trade(
            price_block,rule,run_lengths[price_indexes-2],maximum,
            sell_fee
        )
        key=(score["issued_actions"],-maximum)
        price_fallback.append((
            score["exact_price_wilson_lower_percent"],key,maximum,score
        ))
        if (
            score["issued_actions"]>=PRICE_MIN_ACTIONS
            and score["exact_price_wilson_lower_percent"]>=target*100
        ):
            price_candidates.append((key,maximum,score))
    if price_candidates:
        _,maximum_prior_run,price_calibration=max(
            price_candidates,key=lambda item:item[0]
        )
        price_calibration_passed=True
    else:
        _,_,maximum_prior_run,price_calibration=max(
            price_fallback,key=lambda item:(item[0],item[1])
        )
        price_calibration_passed=False
    horizon_candidates=[]
    horizon_fallback=[]
    for horizon in PRICE_HORIZON_CANDIDATES:
        score=score_trade_horizon(
            price_block,rule,run_lengths[price_indexes-2],
            maximum_prior_run,run_lengths[price_indexes],sell_fee,
            horizon
        )
        horizon_fallback.append((
            score["within_horizon_wilson_lower_percent"],-horizon,
            horizon,score
        ))
        if (
            score["events"]>=20
            and score["within_horizon_wilson_lower_percent"]>=target*100
        ):
            horizon_candidates.append((horizon,score))
    if horizon_candidates:
        horizon,horizon_calibration=min(
            horizon_candidates,key=lambda item:item[0]
        )
        horizon_calibration_passed=True
    else:
        _,_,horizon,horizon_calibration=max(horizon_fallback)
        horizon_calibration_passed=False
    test_indexes=np.arange(test_start,count)
    price_test=score_precision_trade(
        test_block,rule,run_lengths[test_indexes-2],maximum_prior_run,
        sell_fee
    )
    horizon_test=score_trade_horizon(
        test_block,rule,run_lengths[test_indexes-2],maximum_prior_run,
        run_lengths[test_indexes],sell_fee,horizon
    )
    return({
        "rule":rule,
        "target_percent":target*100,
        "calibration_required_lower_percent":required,
        "calibration_passed":calibration_passed,
        "calibration_folds":fold_scores,
        "test":test,
        "test_passed":(
            test["action_wilson_lower_percent"]>=target*100
        ),
        "event_level_test_passed":(
            test["positive_event_wilson_lower_percent"]>=target*100
        ),
        "deployment_ready":(
            calibration_passed
            and test["action_wilson_lower_percent"]>=target*100
            and test["positive_event_wilson_lower_percent"]>=target*100
        ),
        "test_start":test_start,
        "conditional_rule":conditional_rule,
        "conditional_test":conditional_test,
        "price_tier":{
            "maximum_prior_run":maximum_prior_run,
            "calibration_passed":price_calibration_passed,
            "calibration":price_calibration,
            "test":price_test,
            "horizon_windows":horizon,
            "horizon_calibration_passed":horizon_calibration_passed,
            "horizon_calibration":horizon_calibration,
            "horizon_test":horizon_test,
            "horizon_test_passed":(
                horizon_test["within_horizon_wilson_lower_percent"]
                >=target*100
            ),
            "test_passed":(
                price_test["exact_price_wilson_lower_percent"]>=target*100
            ),
            "event_level_test_passed":(
                price_test["positive_event_wilson_lower_percent"]>=target*100
            ),
            "deployment_ready":(
                calibration_passed
                and price_calibration_passed
                and horizon_calibration_passed
                and price_test["exact_price_wilson_lower_percent"]>=target*100
                and price_test["positive_event_wilson_lower_percent"]>=target*100
                and horizon_test["within_horizon_wilson_lower_percent"]
                >=target*100
            )
        }
    })


def live_event_signals(data,changes,event_model,sell_fee):
    states=cents(data["states"])
    scale=event_scale(changes,len(changes))
    previous=changes[-1]
    earlier=changes[-2]
    previous_moved=previous!=0
    strength=np.abs(previous)/scale
    chain_fraction=float(np.sum(
        (previous==-earlier)&previous_moved
    )/max(1,np.sum(previous_moved)))
    strong_count=int(np.sum(strength>=EVENT_STRONG_Z))
    rule=event_model["rule"]
    active=(
        chain_fraction>=rule["chain_fraction"]
        and strong_count>=rule["strong_count"]
    )
    verified_mask=(
        previous_moved&(strength>=rule["cell_z"])
        if active else np.zeros(len(previous),dtype=bool)
    )
    conditional_mask=previous_moved&(
        strength>=event_model["conditional_rule"]["cell_z"]
    )
    source_run=int(data["run_lengths"][-3])
    maximum_prior_run=event_model["price_tier"]["maximum_prior_run"]
    horizon=event_model["price_tier"]["horizon_windows"]
    price_active=active and source_run<=maximum_prior_run
    trade_mask=(
        verified_mask
        &(np.abs(previous)/states[-1]>=sell_fee)
        if price_active else np.zeros(len(previous),dtype=bool)
    )

    def make_signals(mask):
        signals=[]
        for index in np.flatnonzero(mask):
            predicted_delta=-int(previous[index])
            predicted_return=predicted_delta/states[-1,index]
            estimated_net=(
                (1+predicted_return)*(1-sell_fee)-1
                if predicted_delta>0
                else (1-sell_fee)/(1+predicted_return)-1
            )
            signals.append({
                "stock":data["stock_ids"][index],
                "current_price":float(states[-1,index]/100),
                "direction":"UP" if predicted_delta>0 else "DOWN",
                "estimated_price":float(
                    (states[-1,index]+predicted_delta)/100
                ),
                "estimated_change":float(predicted_delta/100),
                "strength":float(strength[index]),
                "estimated_net_bps":float(estimated_net*10000)
            })
        signals.sort(key=lambda item:item["strength"],reverse=True)
        return(signals)

    return({
        "chain_fraction":chain_fraction,
        "strong_stock_count":strong_count,
        "verified_regime_active":bool(active),
        "price_source_run":source_run,
        "price_regime_active":bool(price_active),
        "price_horizon_windows":horizon,
        "price_window_start_timestamp":int(
            data["maximum_timestamp"]+INTERVAL
        ),
        "price_window_end_timestamp":int(
            data["maximum_timestamp"]+horizon*INTERVAL
        ),
        "verified_signals":make_signals(verified_mask),
        "trade_signals":make_signals(trade_mask),
        "conditional_signals":make_signals(conditional_mask)
    })


def fit_gate_hazards(run_lengths):
    maximum=int(np.max(run_lengths))
    hazards=np.zeros(maximum+1,dtype=np.float64)
    for age in range(1,maximum+1):
        at_risk=np.sum(run_lengths>=age)
        hazards[age]=np.sum(run_lengths==age)/max(1,at_risk)
    return(hazards)


def score_gate(run_lengths,positive_ages):
    positive_ages=np.asarray(positive_ages,dtype=np.int64)
    predicted=sum(
        int(np.sum(run_lengths>=age)) for age in positive_ages
    )
    true_positive=int(np.sum(np.isin(run_lengths,positive_ages)))
    false_positive=predicted-true_positive
    false_negative=len(run_lengths)-true_positive
    total=int(np.sum(run_lengths))
    true_negative=total-true_positive-false_positive-false_negative
    return({
        "windows":total,
        "true_positive":true_positive,
        "false_positive":false_positive,
        "true_negative":true_negative,
        "false_negative":false_negative,
        "accuracy_percent":(
            true_positive+true_negative
        )/max(1,total)*100,
        "precision_percent":true_positive/max(1,predicted)*100,
        "recall_percent":true_positive/max(1,len(run_lengths))*100,
        "f1_percent":2*true_positive/max(
            1,2*true_positive+false_positive+false_negative
        )*100
    })


def build_gate(data):
    completed=np.asarray(data["run_lengths"][:-1],dtype=np.int64)
    count=len(completed)
    train_end=int(count*.60)
    validation_end=int(count*.80)
    hazards=fit_gate_hazards(completed[:train_end])
    candidates=[]
    for threshold in np.unique(hazards[1:]):
        positive=np.flatnonzero(hazards>=threshold)
        positive=positive[positive>0]
        score=score_gate(completed[train_end:validation_end],positive)
        candidates.append((
            score["accuracy_percent"],score["f1_percent"],
            score["precision_percent"],threshold,positive,score
        ))
    _,_,_,threshold,positive_ages,validation=max(candidates,key=lambda item:item[:3])
    test=score_gate(completed[validation_end:],positive_ages)
    live_hazards=fit_gate_hazards(completed)
    current_age=int(data["run_lengths"][-1])
    probability=(
        float(live_hazards[current_age])
        if current_age<len(live_hazards) else 1.0
    )
    future=positive_ages[positive_ages>=current_age]
    predicted_age=int(future[0]) if len(future) else current_age+1
    timestamp=int(data["state_starts"][-1]+predicted_age*INTERVAL)
    return({
        "threshold":float(threshold),
        "positive_ages":[int(age) for age in positive_ages],
        "validation":validation,
        "test":test,
        "current_age":current_age,
        "next_window_change_probability_percent":probability*100,
        "predicts_next_window_change":current_age in positive_ages,
        "estimated_change_age":predicted_age,
        "estimated_change_timestamp":timestamp,
        "estimated_change_time":datetime.fromtimestamp(timestamp).isoformat(
            sep=" ",timespec="minutes"
        )
    })


def score_gate_event_intersection(data,changes,event_model,gate):
    start=event_model["test_start"]
    indexes=np.arange(start,len(changes))
    block=prepare_event_block(
        cents(data["states"]),changes,indexes,event_scale(changes,start),
        data["state_starts"]
    )
    active,issued=event_rule_masks(block,event_model["rule"])
    gate_hit=np.isin(
        data["run_lengths"][indexes],gate["positive_ages"]
    )
    combined=active&gate_hit
    issued=issued&combined[:,None]
    correct=int(np.sum(issued&block["correct_direction"]))
    actions=int(np.sum(issued))
    return({
        "events":int(np.sum(combined)),
        "actions":actions,
        "correct_actions":correct,
        "action_accuracy_percent":correct/max(1,actions)*100
    })


def build_change_pipeline(data,target,sell_fee):
    states=cents(data["states"])
    changes=np.diff(states,axis=0)
    event_model=select_event_rule(
        states,changes,target,data["state_starts"],
        data["run_lengths"],sell_fee
    )
    gate=build_gate(data)
    return({
        "gate":gate,
        "event_model":event_model,
        "general_gate_event_intersection":score_gate_event_intersection(
            data,changes,event_model,gate
        ),
        "live":live_event_signals(data,changes,event_model,sell_fee)
    })


def forward_time(timestamp):
    return(datetime.fromtimestamp(int(timestamp)).isoformat(
        sep=" ",timespec="minutes"
    ))


def canonical_history(data,cutoff):
    timestamps=data["timestamps"]
    count=int(np.searchsorted(timestamps,int(cutoff),side="right"))
    if count<1:
        raise ValueError("forward cutoff precedes the cache history")
    cumulative=np.cumsum(data["run_lengths"])
    run_count=int(np.searchsorted(cumulative,count,side="left"))+1
    lengths=data["run_lengths"][:run_count].copy()
    lengths[-1]=count-int(np.sum(lengths[:-1]))
    prices=cents(data["states"][:run_count])
    digest=hashlib.sha256()
    digest.update(FORWARD_SCHEMA.encode("ascii"))
    digest.update(json.dumps(
        list(data["stock_ids"]),separators=(",",":"),ensure_ascii=False
    ).encode("utf-8"))
    digest.update(np.asarray(
        timestamps[:count],dtype="<i8"
    ).tobytes())
    digest.update(np.asarray(lengths,dtype="<i8").tobytes())
    digest.update(np.asarray(prices,dtype="<i8").tobytes())
    return({
        "sha256":digest.hexdigest(),
        "timestamp_count":count,
        "distinct_states":run_count,
        "last_timestamp":int(timestamps[count-1])
    })


def clean_event_indexes(data,indexes):
    indexes=np.asarray(indexes,dtype=np.int64)
    if not len(indexes):
        return(indexes,np.zeros(0,dtype=bool))
    starts=data["state_starts"]
    ends=data["state_ends"]
    lengths=data["run_lengths"]
    contiguous=(ends-starts)==(lengths-1)*INTERVAL
    valid=(
        contiguous[indexes-2]
        &contiguous[indexes-1]
        &contiguous[indexes]
        &(starts[indexes-1]-ends[indexes-2]==INTERVAL)
        &(starts[indexes]-ends[indexes-1]==INTERVAL)
        &(starts[indexes+1]-ends[indexes]==INTERVAL)
    )
    return(indexes[valid],valid)


def create_forward_state(data,pipeline,cache_file,forward_file):
    if forward_file.exists():
        raise ValueError(
            f"forward state already exists: {forward_file}"
        )
    states=cents(data["states"])
    changes=np.diff(states,axis=0)
    model=pipeline["event_model"]
    history=canonical_history(data,data["maximum_timestamp"])
    sell_fee=float(model["price_tier"]["test"]["sell_fee_percent"])/100
    anchors=[]
    for index in range(max(0,len(states)-3),len(states)):
        anchors.append({
            "state_start":int(data["state_starts"][index]),
            "prices_cents":[int(value) for value in states[index]]
        })
    payload={
        "schema":FORWARD_SCHEMA,
        "version":FORWARD_VERSION,
        "experiment_id":str(uuid.uuid4()),
        "created_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "cache_file":str(cache_file),
        "interval_seconds":INTERVAL,
        "cutoff_timestamp":int(data["maximum_timestamp"]),
        "cutoff_time":forward_time(data["maximum_timestamp"]),
        "cutoff_state_start":int(data["state_starts"][-1]),
        "stock_ids":list(data["stock_ids"]),
        "training_history":history,
        "scale_cents":[
            float(value) for value in event_scale(changes,len(changes))
        ],
        "event_strong_z":EVENT_STRONG_Z,
        "event_rule":dict(model["rule"]),
        "price_tier":{
            "maximum_prior_run":int(
                model["price_tier"]["maximum_prior_run"]
            ),
            "horizon_windows":int(
                model["price_tier"]["horizon_windows"]
            ),
            "sell_fee":sell_fee,
            "stress_sell_fee":min(.999,sell_fee*2)
        },
        "gate_positive_ages":list(pipeline["gate"]["positive_ages"]),
        "target_percent":float(model["target_percent"]),
        "minimum_distinct_days":FORWARD_MIN_DAYS,
        "minimum_event_clusters":FORWARD_MIN_CLUSTERS,
        "minimum_calendar_days":FORWARD_MIN_CALENDAR_DAYS,
        "independent_cluster_gap_seconds":FORWARD_CLUSTER_GAP,
        "anchors":anchors
    }
    forward_file.parent.mkdir(parents=True,exist_ok=True)
    text=json.dumps(payload,indent=2)+"\n"
    with forward_file.open("x",encoding="utf-8") as file:
        file.write(text)
    return(payload)


def load_forward_state(forward_file):
    if not forward_file.is_file():
        raise FileNotFoundError(forward_file)
    with forward_file.open("r",encoding="utf-8") as file:
        state=json.load(file)
    if (
        state.get("schema")!=FORWARD_SCHEMA
        or state.get("version")!=FORWARD_VERSION
    ):
        raise ValueError(
            "unsupported frozen forward-state format"
        )
    if state.get("interval_seconds")!=INTERVAL:
        raise ValueError("frozen forward interval is not 15 minutes")
    return(state)


def validate_forward_history(data,state):
    if list(data["stock_ids"])!=state["stock_ids"]:
        raise ValueError("forward stock order or membership changed")
    if data["maximum_timestamp"]<state["cutoff_timestamp"]:
        raise ValueError("cache ends before the frozen forward cutoff")
    actual_history=canonical_history(data,state["cutoff_timestamp"])
    expected_history=state["training_history"]
    if actual_history!=expected_history:
        raise ValueError(
            "cache history at or before the frozen cutoff changed"
        )
    starts={
        int(timestamp):index
        for index,timestamp in enumerate(data["state_starts"])
    }
    prices=cents(data["states"])
    for anchor in state["anchors"]:
        timestamp=int(anchor["state_start"])
        if timestamp not in starts:
            raise ValueError(
                f"frozen history anchor is missing: {timestamp}"
            )
        actual=prices[starts[timestamp]]
        expected=np.asarray(anchor["prices_cents"],dtype=np.int64)
        if not np.array_equal(actual,expected):
            raise ValueError(
                f"frozen history anchor changed: {timestamp}"
            )


def perfect_results_needed(correct,total,target):
    additional=0
    while wilson_lower(correct+additional,total+additional)<target:
        additional+=1
        if additional>100000:
            return(None)
    return(additional)


def forward_event_details(
    data,block,indexes,rule,maximum_prior_run,horizon,sell_fee,
    stress_sell_fee
):
    active,issued=event_rule_masks(block,rule)
    price_active=active&(
        data["run_lengths"][indexes-2]<=maximum_prior_run
    )
    issued=(
        issued&price_active[:,None]
        &(np.abs(block["previous"])/block["current"]>=sell_fee)
    )
    predicted_direction=-np.sign(block["previous"])
    actual_return=block["actual"]/block["current"]
    net_return=np.where(
        predicted_direction>0,
        (1+actual_return)*(1-sell_fee)-1,
        (1-sell_fee)/(1+actual_return)-1
    )
    stress_return=np.where(
        predicted_direction>0,
        (1+actual_return)*(1-stress_sell_fee)-1,
        (1-stress_sell_fee)/(1+actual_return)-1
    )
    details=[]
    for row in np.flatnonzero(np.any(issued,axis=1)):
        mask=issued[row]
        exact=block["exact"][row]
        values=net_return[row][mask]
        stress_values=stress_return[row][mask]
        index=int(indexes[row])
        details.append({
            "entry_timestamp":int(data["state_starts"][index]),
            "entry_time":forward_time(data["state_starts"][index]),
            "result_timestamp":int(data["state_starts"][index+1]),
            "result_time":forward_time(data["state_starts"][index+1]),
            "run_windows":int(data["run_lengths"][index]),
            "within_horizon":bool(data["run_lengths"][index]<=horizon),
            "actions":int(np.sum(mask)),
            "exact_prices":int(np.sum(mask&exact)),
            "all_issued_prices_exact":bool(np.all((~mask)|exact)),
            "full_reversal_vector_exact":bool(block["vector_exact"][row]),
            "profitable_actions":int(np.sum(values>0)),
            "mean_net_bps":float(np.mean(values)*10000),
            "mean_stress_net_bps":float(np.mean(stress_values)*10000)
        })
    return(details)


def group_independent_events(details,gap_seconds):
    clusters=[]
    previous=None
    for event in details:
        timestamp=int(event["result_timestamp"])
        if previous is None or timestamp-previous>=gap_seconds:
            clusters.append([])
        clusters[-1].append(event)
        previous=timestamp
    return(clusters)


def confirmation_metrics(details,state,target,latest_timestamp):
    minimum_days=int(state.get("minimum_distinct_days",FORWARD_MIN_DAYS))
    minimum_clusters=int(
        state.get("minimum_event_clusters",FORWARD_MIN_CLUSTERS)
    )
    minimum_calendar=int(
        state.get("minimum_calendar_days",FORWARD_MIN_CALENDAR_DAYS)
    )
    gap=int(state.get(
        "independent_cluster_gap_seconds",FORWARD_CLUSTER_GAP
    ))
    cutoff=int(state["cutoff_timestamp"])
    days=set()
    cluster_count=0
    previous=None
    count_time=None
    for event in details:
        timestamp=int(event["result_timestamp"])
        if previous is None or timestamp-previous>=gap:
            cluster_count+=1
        days.add(timestamp//86400)
        previous=timestamp
        if (
            cluster_count>=minimum_clusters
            and len(days)>=minimum_days
        ):
            count_time=timestamp
            break
    endpoint=(
        max(count_time,cutoff+minimum_calendar*86400)
        if count_time is not None else None
    )
    coverage=endpoint is not None and int(latest_timestamp)>=endpoint
    selected=(
        [
            event for event in details
            if int(event["result_timestamp"])<=endpoint
        ]
        if coverage else details
    )
    clusters=group_independent_events(selected,gap)
    exact_clusters=0
    timing_clusters=0
    positive_clusters=0
    stress_positive_clusters=0
    cluster_rows=[]
    for cluster in clusters:
        actions=sum(event["actions"] for event in cluster)
        exact=sum(event["exact_prices"] for event in cluster)
        timing=sum(event["within_horizon"] for event in cluster)
        base_return=float(np.prod([
            1+event["mean_net_bps"]/10000 for event in cluster
        ])-1)
        stress_return=float(np.prod([
            1+event["mean_stress_net_bps"]/10000 for event in cluster
        ])-1)
        exact_success=actions>0 and exact/actions>=target
        timing_success=timing/len(cluster)>=target
        exact_clusters+=int(exact_success)
        timing_clusters+=int(timing_success)
        positive_clusters+=int(base_return>0)
        stress_positive_clusters+=int(stress_return>0)
        cluster_rows.append({
            "start_timestamp":int(cluster[0]["result_timestamp"]),
            "end_timestamp":int(cluster[-1]["result_timestamp"]),
            "events":len(cluster),
            "actions":actions,
            "exact_endpoint_percent":exact/max(1,actions)*100,
            "within_horizon_percent":timing/len(cluster)*100,
            "compound_net_bps":base_return*10000,
            "stress_compound_net_bps":stress_return*10000
        })
    total=len(clusters)
    distinct_days=len({
        int(event["result_timestamp"])//86400 for event in selected
    })
    calendar_days=max(
        0,((endpoint if coverage else int(latest_timestamp))-cutoff)//86400
    )
    metrics={
        "events":len(selected),
        "independent_clusters":total,
        "distinct_utc_days":distinct_days,
        "calendar_days":calendar_days,
        "exact_target_clusters":exact_clusters,
        "exact_target_cluster_percent":exact_clusters/max(1,total)*100,
        "exact_target_wilson_lower_percent":wilson_lower(
            exact_clusters,total
        )*100,
        "within_horizon_clusters":timing_clusters,
        "within_horizon_cluster_percent":timing_clusters/max(1,total)*100,
        "within_horizon_wilson_lower_percent":wilson_lower(
            timing_clusters,total
        )*100,
        "positive_net_clusters":positive_clusters,
        "positive_net_cluster_percent":positive_clusters/max(1,total)*100,
        "positive_net_wilson_lower_percent":wilson_lower(
            positive_clusters,total
        )*100,
        "stress_positive_net_clusters":stress_positive_clusters,
        "stress_positive_net_cluster_percent":stress_positive_clusters/max(
            1,total
        )*100,
        "stress_positive_net_wilson_lower_percent":wilson_lower(
            stress_positive_clusters,total
        )*100,
        "clusters":cluster_rows
    }
    passed=(
        coverage
        and metrics["exact_target_wilson_lower_percent"]>=target*100
        and metrics["within_horizon_wilson_lower_percent"]>=target*100
        and metrics["positive_net_wilson_lower_percent"]>=target*100
        and metrics["stress_positive_net_wilson_lower_percent"]>=target*100
    )
    return({
        "coverage_complete":coverage,
        "passed":passed,
        "endpoint_timestamp":endpoint if coverage else None,
        "endpoint_time":forward_time(endpoint) if coverage else None,
        "minimum_independent_clusters":minimum_clusters,
        "minimum_distinct_utc_days":minimum_days,
        "minimum_calendar_days":minimum_calendar,
        "cluster_gap_hours":gap/3600,
        "metrics":metrics,
        "requirements":{
            "more_independent_clusters":max(0,minimum_clusters-total),
            "more_distinct_utc_days":max(0,minimum_days-distinct_days),
            "more_calendar_days":max(0,minimum_calendar-calendar_days),
            "more_perfect_exact_clusters":perfect_results_needed(
                exact_clusters,total,target
            ),
            "more_perfect_timing_clusters":perfect_results_needed(
                timing_clusters,total,target
            ),
            "more_perfect_positive_clusters":perfect_results_needed(
                positive_clusters,total,target
            ),
            "more_perfect_stress_positive_clusters":perfect_results_needed(
                stress_positive_clusters,total,target
            )
        }
    })


def frozen_current_prediction(data,state,scale):
    states=cents(data["states"])
    changes=np.diff(states,axis=0)
    contract={
        "target":"next_distinct_changed_state",
        "immediate_15_minute_window":False,
        "minimum_distinct_states":3,
        "frozen_scale_values":len(scale),
        "fixed_raw_snapshot_count":None
    }
    if len(states)<3:
        return({
            "decision":"INSUFFICIENT_CONTEXT",
            "contract":contract,
            "reasons":["fewer than three distinct stock states"]
        })
    rule=state["event_rule"]
    price_state=state["price_tier"]
    previous=changes[-1]
    earlier=changes[-2]
    moved=previous!=0
    strength=np.abs(previous)/scale
    moved_count=max(1,int(np.sum(moved)))
    chain_fraction=float(np.sum(
        (previous==-earlier)&moved
    )/moved_count)
    strong_count=int(np.sum(
        strength>=float(state["event_strong_z"])
    ))
    prior_prior_run=int(data["run_lengths"][-3])
    event_active=(
        chain_fraction>=float(rule["chain_fraction"])
        and strong_count>=int(rule["strong_count"])
    )
    run_eligible=(
        prior_prior_run<=int(price_state["maximum_prior_run"])
    )
    sell_fee=float(price_state["sell_fee"])
    issued=(
        moved
        &(strength>=float(rule["cell_z"]))
        &(np.abs(previous)/states[-1]>=sell_fee)
    )
    age=int(data["run_lengths"][-1])
    horizon=int(price_state["horizon_windows"])
    horizon_open=age<=horizon
    qualified=event_active and run_eligible and bool(np.any(issued))
    actionable=qualified and horizon_open
    reasons=[]
    if not event_active:
        if chain_fraction<float(rule["chain_fraction"]):
            reasons.append(
                f"chain fraction {chain_fraction:.3f} is below "
                f"{float(rule['chain_fraction']):.3f}"
            )
        if strong_count<int(rule["strong_count"]):
            reasons.append(
                f"{strong_count} strong stocks is below "
                f"{int(rule['strong_count'])}"
            )
    if not run_eligible:
        reasons.append(
            f"two-back state lasted {prior_prior_run} snapshots; maximum is "
            f"{int(price_state['maximum_prior_run'])}"
        )
    if not np.any(issued):
        reasons.append("no stock move clears the sell-fee threshold")
    if qualified and not horizon_open:
        reasons.append(
            f"current state age {age} is beyond the {horizon}-window horizon"
        )
    actions=[]
    predicted=states[-1]-previous
    for column in np.flatnonzero(issued if actionable else np.zeros_like(issued)):
        actions.append({
            "stock":data["stock_ids"][column],
            "direction":"BUY" if previous[column]<0 else "SELL_OR_AVOID",
            "current_price":float(states[-1,column]/100),
            "predicted_price":float(predicted[column]/100)
        })
    return({
        "decision":"PREDICT" if actionable else "ABSTAIN",
        "contract":contract,
        "context_ready":True,
        "state_start_timestamp":int(data["state_starts"][-1]),
        "observed_through_timestamp":int(data["maximum_timestamp"]),
        "current_state_age_snapshots":age,
        "horizon_windows_from_state_start":horizon,
        "chain_fraction":chain_fraction,
        "strong_stocks":strong_count,
        "two_back_state_snapshots":prior_prior_run,
        "event_rule_active":event_active,
        "price_tier_eligible":run_eligible,
        "horizon_open":horizon_open,
        "candidate_actions":int(np.sum(issued)),
        "actions":actions,
        "reasons":reasons
    })


def evaluate_forward(data,state):
    validate_forward_history(data,state)
    states=cents(data["states"])
    changes=np.diff(states,axis=0)
    cutoff=int(state["cutoff_timestamp"])
    raw_indexes=np.flatnonzero(data["state_starts"][1:]>cutoff)
    raw_indexes=raw_indexes[raw_indexes>=2]
    indexes,clean_mask=clean_event_indexes(data,raw_indexes)
    scale=np.asarray(state["scale_cents"],dtype=np.float64)
    if len(scale)!=states.shape[1]:
        raise ValueError("frozen forward scale does not match stock count")
    block=prepare_event_block(
        states,changes,indexes,scale,data["state_starts"],
        float(state["event_strong_z"])
    )
    rule=dict(state["event_rule"])
    price_state=state["price_tier"]
    maximum_prior_run=int(price_state["maximum_prior_run"])
    horizon=int(price_state["horizon_windows"])
    sell_fee=float(price_state["sell_fee"])
    stress_sell_fee=float(price_state["stress_sell_fee"])
    base=score_event_rule(block,rule)
    price=score_precision_trade(
        block,rule,data["run_lengths"][indexes-2],maximum_prior_run,
        sell_fee
    )
    timing=score_trade_horizon(
        block,rule,data["run_lengths"][indexes-2],maximum_prior_run,
        data["run_lengths"][indexes],sell_fee,horizon
    )
    active,issued=event_rule_masks(block,rule)
    gate_hit=np.isin(
        data["run_lengths"][indexes],state["gate_positive_ages"]
    )
    combined=active&gate_hit
    combined_issued=issued&combined[:,None]
    combined_actions=int(np.sum(combined_issued))
    combined_correct=int(np.sum(
        combined_issued&block["correct_direction"]
    ))
    target=float(state["target_percent"])/100
    details=forward_event_details(
        data,block,indexes,rule,maximum_prior_run,horizon,sell_fee,
        stress_sell_fee
    )
    confirmation=confirmation_metrics(
        details,state,target,data["maximum_timestamp"]
    )
    status=(
        "READY" if confirmation["passed"]
        else "FAILED" if confirmation["coverage_complete"]
        else "COLLECTING"
    )
    return({
        "status":status,
        "deployment_ready":bool(confirmation["passed"]),
        "experiment_id":state["experiment_id"],
        "frozen_at":state["created_at"],
        "cutoff_timestamp":cutoff,
        "cutoff_time":state["cutoff_time"],
        "latest_timestamp":int(data["maximum_timestamp"]),
        "latest_time":forward_time(data["maximum_timestamp"]),
        "fee_model":"sell_only_once",
        "current_prediction":frozen_current_prediction(
            data,state,scale
        ),
        "post_cutoff_grid_windows":max(
            0,(int(data["maximum_timestamp"])-cutoff)//INTERVAL
        ),
        "post_cutoff_changed_events":len(raw_indexes),
        "scorable_changed_events":len(indexes),
        "gap_excluded_changed_events":int(np.sum(~clean_mask)),
        "base_direction_tier":base,
        "precision_trade_tier":price,
        "timing_range":timing,
        "general_gate_intersection":{
            "events":int(np.sum(combined)),
            "actions":combined_actions,
            "correct_actions":combined_correct,
            "action_accuracy_percent":combined_correct/max(
                1,combined_actions
            )*100
        },
        "confirmation":confirmation,
        "requirements":confirmation["requirements"],
        "qualifying_events":details
    })


def print_forward_freeze(state,forward_file):
    print("\nForward test frozen")
    print(f"State file: {forward_file}")
    print(
        f"Cutoff: {state['cutoff_time']} ({state['cutoff_timestamp']}) | "
        f"target {state['target_percent']:g}%"
    )
    print(
        "Rules and normalization scales are now immutable; later runs score "
        "only changed states after this cutoff."
    )
    print(
        f"Confirmation requires {state['minimum_event_clusters']} independent "
        f"24-hour clusters across {state['minimum_distinct_days']} UTC days "
        f"and {state['minimum_calendar_days']} calendar days."
    )
    command=(
        "stocks_algorithm_lab.exe --forward-test"
        if getattr(sys,"frozen",False)
        else "python stocks_algorithm_lab.py --forward-test"
    )
    print(f"Run: {command}")


def print_forward_report(result):
    price=result["precision_trade_tier"]
    timing=result["timing_range"]
    current=result["current_prediction"]
    confirmation=result["confirmation"]
    metrics=confirmation["metrics"]
    print("\nFrozen forward-test report")
    print(
        f"Cutoff: {result['cutoff_time']} | latest: {result['latest_time']} | "
        f"{result['post_cutoff_grid_windows']:,} post-cutoff windows | "
        f"{result['post_cutoff_changed_events']:,} changed events | "
        f"{result['gap_excluded_changed_events']:,} gap-excluded"
    )
    print(
        f"Precision tier: {price['events']} events / "
        f"{price['issued_actions']} actions | exact vectors "
        f"{price['full_reversal_exact_vectors']}/{price['events']} | "
        f"net-positive events {price['positive_events']}/{price['events']}"
    )
    print(
        f"Timing <= {timing['horizon_windows']} windows: "
        f"{timing['within_horizon']}/{timing['events']}"
    )
    print(
        f"Current next-distinct-state decision: {current['decision']} | "
        f"state age {current.get('current_state_age_snapshots',0)} snapshots"
    )
    if current.get("reasons"):
        print(f"Reason: {'; '.join(current['reasons'])}")
    if price["issued_actions"]:
        print(
            f"Exact endpoints {price['exact_price_percent']:.3f}% | "
            f"net-profitable actions {price['profitable_action_percent']:.3f}% | "
            f"mean net {price['mean_net_bps']:.3f} bps"
        )
    print(
        f"Independent confirmation: {metrics['independent_clusters']} clusters "
        f"across {metrics['distinct_utc_days']} UTC days / "
        f"{metrics['calendar_days']} calendar days"
    )
    if metrics["independent_clusters"]:
        print(
            "Cluster Wilson lower bounds: "
            f"exact {metrics['exact_target_wilson_lower_percent']:.3f}% | "
            f"timing {metrics['within_horizon_wilson_lower_percent']:.3f}% | "
            f"net-positive {metrics['positive_net_wilson_lower_percent']:.3f}% | "
            "double-fee net-positive "
            f"{metrics['stress_positive_net_wilson_lower_percent']:.3f}%"
        )
    requirements=result["requirements"]
    print(
        f"Status: {result['status']} | coverage still needed: "
        f"{requirements['more_independent_clusters']} clusters / "
        f"{requirements['more_distinct_utc_days']} UTC days / "
        f"{requirements['more_calendar_days']} calendar days"
    )
    if result["status"]=="FAILED":
        print("The frozen confirmation failed; this experiment will not retune.")
    if not result["qualifying_events"]:
        print("No new qualifying precision-tier event has completed yet.")
        return
    print(
        f"{'RESULT TIME':<18}{'RUN':>6}{'ACTIONS':>9}{'EXACT':>9}"
        f"{'PROFIT':>9}{'NET BPS':>10}"
    )
    for event in result["qualifying_events"][-10:]:
        print(
            f"{event['result_time']:<18}{event['run_windows']:>6}"
            f"{event['actions']:>9}{event['exact_prices']:>9}"
            f"{event['profitable_actions']:>9}"
            f"{event['mean_net_bps']:>10.2f}"
        )


def predict_latest(
    data,selected,hazards,ar2_coefficients,transition_results,changed_only
):
    states=data["states"]
    latest_index=len(states)-1
    current=states[latest_index]
    age=int(data["run_lengths"][-1])
    hazard=hazard_at(hazards,age)
    completed=data["run_lengths"][:-1]
    eligible=completed[completed>=age]
    expected_length=(
        int(np.bincount(eligible).argmax())
        if len(eligible)
        else age
    )
    estimated_change_timestamp=(
        data["maximum_timestamp"]
        +max(1,expected_length-age+1)*INTERVAL
    )
    use_transition=(
        selected["model"]!="persistence"
        and hazard>=selected["threshold"]
    )
    primary=transition_predictions(
        states,np.asarray([latest_index]),selected["model"],ar2_coefficients
    )[0] if use_transition else current.copy()

    conditional_model=transition_results[0]["model"]
    conditional=transition_predictions(
        states,np.asarray([latest_index]),conditional_model,ar2_coefficients
    )[0]
    if changed_only:
        primary=conditional.copy()
    return({
        "timestamp":data["maximum_timestamp"]+INTERVAL,
        "estimated_change_timestamp":estimated_change_timestamp,
        "age":age,
        "change_probability_percent":hazard*100,
        "primary":np.round(primary,2),
        "conditional":np.round(conditional,2),
        "conditional_model":conditional_model,
        "current":current
    })


def serializable_result(
    data,structure,candidates,transitions,selected,prediction,
    examples,changed_only
):
    stocks=[]
    for index,stock_id in enumerate(data["stock_ids"]):
        current=float(prediction["current"][index])
        predicted=float(prediction["primary"][index])
        conditional=float(prediction["conditional"][index])
        stocks.append({
            "stock":stock_id,
            "current_price":current,
            "predicted_price":predicted,
            "predicted_change":round(predicted-current,2),
            "conditional_update_price":conditional,
            "conditional_update_change":round(conditional-current,2)
        })
    active_model=transitions[0] if changed_only else selected
    target_timestamp=(
        prediction["estimated_change_timestamp"]
        if changed_only
        else prediction["timestamp"]
    )
    return({
        "structure":structure,
        "selected_model":active_model,
        "leaderboard":candidates[:10],
        "transition_only_leaderboard":transitions,
        "next_timestamp":target_timestamp,
        "next_time":datetime.fromtimestamp(target_timestamp).isoformat(
            sep=" ",timespec="minutes"
        ),
        "target_kind":(
            "next_distinct_changed_state"
            if changed_only
            else "next_15_minute_window"
        ),
        "current_run_age_windows":prediction["age"],
        "next_window_change_probability_percent":prediction[
            "change_probability_percent"
        ],
        "conditional_update_model":prediction["conditional_model"],
        "changed_only":changed_only,
        "changed_window_examples":examples,
        "predictions":stocks
    })


def print_report(result,top):
    structure=result["structure"]
    selected=result["selected_model"]
    print("\nReverse-engineering summary")
    print(
        f"{structure['windows']:,} authoritative 15-minute windows | "
        f"{structure['years']:.2f} years | {structure['stocks']} stocks"
    )
    print(
        f"Market-vector changes: {structure['changing_window_percent']:.3f}% | "
        f"median hold: {structure['median_hold_hours']:.2f}h | "
        f"mode: {structure['dominant_hold_windows']} windows | "
        f"median stocks changed: {structure['median_stocks_changed']:.0f}"
    )
    print(
        "Best supported family: sparse update gate + persistent, bounded "
        "mean-reverting state with cross-stock factors and cent rounding."
    )
    print(
        f"Observed stock bands: {structure['minimum_price_band_percent']:.3f}%"
        f"-{structure['maximum_price_band_percent']:.3f}% "
        f"(median {structure['median_price_band_percent']:.3f}%)."
    )

    print("\nChronological validation leaderboard")
    print(
        f"{'MODEL':<31}{'VECTOR':>9}{'PRICES':>9}{'GATE':>9}"
        f"{'ON MOVE':>10}{'DIRECTION':>11}{'MAE':>10}"
    )
    for metric in result["leaderboard"][:8]:
        print(
            f"{metric['name']:<31}"
            f"{metric['vector_accuracy_percent']:>8.3f}%"
            f"{metric['price_accuracy_percent']:>8.3f}%"
            f"{metric['move_state_accuracy_percent']:>8.3f}%"
            f"{metric['changed_price_accuracy_percent']:>9.3f}%"
            f"{metric['changed_direction_accuracy_percent']:>10.3f}%"
            f"{metric['mae']:>10.5f}"
        )
    print("\nTransition-only leaderboard")
    print(
        f"{'MODEL':<18}{'VECTOR':>9}{'PRICES':>9}{'ON MOVE':>10}"
        f"{'DIRECTION':>11}{'MAE':>10}"
    )
    for metric in result["transition_only_leaderboard"]:
        print(
            f"{metric['name']:<18}"
            f"{metric['vector_accuracy_percent']:>8.3f}%"
            f"{metric['price_accuracy_percent']:>8.3f}%"
            f"{metric['changed_price_accuracy_percent']:>9.3f}%"
            f"{metric['changed_direction_accuracy_percent']:>10.3f}%"
            f"{metric['mae']:>10.5f}"
        )
    print("\nValidation changed-window examples")
    print(
        f"{'TIME':<18}{'VECTOR':>9}{'EXACT':>10}{'MOVED':>9}"
        f"{'MOVE HIT':>11}{'DIR HIT':>10}{'MAE':>10}"
    )
    for example in result["changed_window_examples"]:
        print(
            f"{example['time']:<18}"
            f"{('YES' if example['exact_vector'] else 'NO'):>9}"
            f"{example['exact_prices']:>7}/35"
            f"{example['changed_prices']:>9}"
            f"{example['exact_changed_prices']:>11}"
            f"{example['correct_changed_directions']:>10}"
            f"{example['mae']:>10.5f}"
        )
    selected_label=(
        "Selected changed-window model"
        if result["changed_only"]
        else "Selected"
    )
    price_accuracy=(
        selected["changed_price_accuracy_percent"]
        if result["changed_only"]
        else selected["price_accuracy_percent"]
    )
    price_accuracy_label=(
        "exact changed-price accuracy"
        if result["changed_only"]
        else "exact individual-price accuracy"
    )
    print(
        f"\n{selected_label}: {selected['name']} | exact full-vector accuracy "
        f"{selected['vector_accuracy_percent']:.3f}% | {price_accuracy_label} "
        f"{price_accuracy:.3f}%"
    )
    if selected["vector_accuracy_percent"]<100:
        print(
            "The held-out data does not support a truthful 100% claim; "
            "the percentage above is the measured result."
        )

    if result["changed_only"]:
        print(
            f"\nEstimated next changed state: {result['next_time']} "
            f"({result['next_timestamp']}) | immediate next-window change "
            f"probability {result['next_window_change_probability_percent']:.2f}%"
        )
    else:
        print(
            f"\nNext window: {result['next_time']} "
            f"({result['next_timestamp']}) | estimated vector-change "
            f"probability {result['next_window_change_probability_percent']:.2f}%"
        )
    print(
        f"Conditional update model: {result['conditional_update_model']}"
    )
    if result["changed_only"]:
        print(
            "Changed-only mode: PREDICTED is the forecast for the next "
            "distinct changed state, not an unchanged-window forecast."
        )
    print(
        f"{'STOCK':<7}{'CURRENT':>11}{'PREDICTED':>12}{'CHANGE':>10}"
        f"{'IF UPDATE':>12}{'UPD CHANGE':>12}"
    )
    predictions=result["predictions"][:top]
    for item in predictions:
        print(
            f"{item['stock']:<7}{item['current_price']:>11.2f}"
            f"{item['predicted_price']:>12.2f}"
            f"{item['predicted_change']:>+10.2f}"
            f"{item['conditional_update_price']:>12.2f}"
            f"{item['conditional_update_change']:>+12.2f}"
        )


def print_change_pipeline(pipeline,top):
    gate=pipeline["gate"]
    gate_test=gate["test"]
    intersection=pipeline["general_gate_event_intersection"]
    model=pipeline["event_model"]
    test=model["test"]
    price=model["price_tier"]["test"]
    horizon=model["price_tier"]["horizon_test"]
    price_calibration=model["price_tier"]["calibration"]
    horizon_calibration=model["price_tier"]["horizon_calibration"]
    rule=model["rule"]
    live=pipeline["live"]
    print("\nTwo-stage change pipeline")
    print(
        f"Gate final test: {gate_test['accuracy_percent']:.3f}% accuracy | "
        f"{gate_test['precision_percent']:.3f}% precision | "
        f"{gate_test['recall_percent']:.3f}% recall | "
        f"{gate_test['true_positive']:,} TP / "
        f"{gate_test['false_positive']:,} FP / "
        f"{gate_test['false_negative']:,} FN"
    )
    print(
        f"Gate ages: {', '.join(map(str,gate['positive_ages']))} | "
        f"current age {gate['current_age']} | next-window change probability "
        f"{gate['next_window_change_probability_percent']:.3f}%"
    )
    print(
        f"Estimated next gate window: {gate['estimated_change_time']} "
        f"({gate['estimated_change_timestamp']})"
    )
    print(
        f"General gate x reversal regime: {intersection['events']} "
        f"intersecting final events / {intersection['actions']} actions; "
        "their headline percentages cannot be multiplied."
    )

    print("\nHigh-confidence event-to-event direction model")
    print(
        f"Learned rule: reversal chain >= {rule['chain_fraction']*100:g}% | "
        f"at least {rule['strong_count']} strong stocks | "
        f"per-stock strength >= {rule['cell_z']:g}"
    )
    print(
        f"Final chronological action rows: {test['action_accuracy_percent']:.3f}% "
        f"({test['correct_actions']:,}/{test['issued_actions']:,}) | "
        f"naive row-level 95% lower bound "
        f"{test['action_wilson_lower_percent']:.3f}% | "
        f"changed-stock direction {test['changed_direction_percent']:.3f}%"
    )
    print(
        f"Coverage: {test['active_events']:,}/{test['events']:,} changed "
        f"events and {test['changed_cell_coverage_percent']:.3f}% of moved "
        f"stock outcomes | exact estimated price {test['exact_price_percent']:.3f}%"
    )
    print(
        f"Dependence check: {test['active_events']} events across "
        f"{test['distinct_days']} days / {test['event_clusters']} clusters | "
        f"positive event portfolios {test['positive_events']}/"
        f"{test['active_events']} | event-level 95% lower bound "
        f"{test['positive_event_wilson_lower_percent']:.3f}%"
    )
    if model["test_passed"]:
        print(
            f"The {model['target_percent']:g}% target passes per action row, "
            "but correlated rows are not independent evidence."
        )
    else:
        print(
            f"Target not passed: this model must abstain from claiming "
            f"{model['target_percent']:g}% reliability."
        )

    print("\nFee-aware precision price tier")
    print(
        f"Rule addition: source run <= {price['maximum_prior_run']} window | "
        f"predicted move >= {price['minimum_predicted_move_bps']:g} bps | "
        f"sale fee {price['sell_fee_percent']:g}%"
    )
    print(
        f"Calibration: exact endpoint "
        f"{price_calibration['exact_price_percent']:.3f}% | change within "
        f"{horizon_calibration['horizon_windows']} windows in "
        f"{horizon_calibration['within_horizon']}/"
        f"{horizon_calibration['events']} events"
    )
    print(
        f"Exact endpoint: {price['exact_price_percent']:.3f}% "
        f"({price['exact_prices']}/{price['issued_actions']}) | "
        f"row-level lower bound {price['exact_price_wilson_lower_percent']:.3f}% | "
        f"full reversal vectors {price['full_reversal_exact_vectors']}/"
        f"{price['events']}"
    )
    print(
        f"Net-profitable actions: {price['profitable_action_percent']:.3f}% "
        f"({price['profitable_actions']}/{price['issued_actions']}) | "
        f"mean {price['mean_net_bps']:.3f} bps | median "
        f"{price['median_net_bps']:.3f} bps"
    )
    print(
        f"Coverage/dependence: {price['events']} events across "
        f"{price['distinct_days']} days / {price['event_clusters']} clusters | "
        f"positive event portfolios {price['positive_events']}/"
        f"{price['events']} | event-level lower bound "
        f"{price['positive_event_wilson_lower_percent']:.3f}%"
    )
    print(
        f"Full-vector lower bound: "
        f"{price['full_reversal_vector_wilson_lower_percent']:.3f}% | "
        f"BUY exact {price['buy']['exact_price_percent']:.3f}% | "
        f"SELL/avoid exact {price['sell_or_avoid']['exact_price_percent']:.3f}%"
    )
    print(
        f"Conditional timing range: within {horizon['horizon_windows']} "
        f"windows in {horizon['within_horizon']}/{horizon['events']} final "
        f"events | event-level lower bound "
        f"{horizon['within_horizon_wilson_lower_percent']:.3f}%"
    )
    print(
        "Net assumes exact cached entry/exit prices, one sale fee, no "
        "latency or slippage; DOWN means sell now and rebuy next change."
    )
    if not model["price_tier"]["deployment_ready"]:
        print(
            "Deployment check: NOT READY; independent event-level evidence "
            "does not yet clear the requested target."
        )

    print(
        f"Current context: reversal chain {live['chain_fraction']*100:.1f}% | "
        f"{live['strong_stock_count']} strong stocks | price source run "
        f"{live['price_source_run']}"
    )
    if live["price_regime_active"] and model["price_tier"]["deployment_ready"]:
        signals=live["trade_signals"]
        label="TRADE"
        print(f"Precision trade regime active: {len(signals)} signals.")
        print(
            f"Expected change range: "
            f"{datetime.fromtimestamp(live['price_window_start_timestamp']).isoformat(sep=' ',timespec='minutes')}"
            f" to "
            f"{datetime.fromtimestamp(live['price_window_end_timestamp']).isoformat(sep=' ',timespec='minutes')}"
        )
    elif live["verified_regime_active"] and model["deployment_ready"]:
        signals=live["verified_signals"]
        label="DIRECTION"
        print(f"Direction regime active: {len(signals)} next-change signals.")
    else:
        signals=live["conditional_signals"]
        label="CONDITIONAL"
        conditional=model["conditional_test"]
        print(
            "Selective regime inactive: ABSTAIN at the high-confidence tier."
        )
        print(
            f"Conditional changed-stock tier: "
            f"{conditional['changed_direction_percent']:.3f}% direction when "
            f"the selected stock actually moves; all-action accuracy "
            f"{conditional['action_accuracy_percent']:.3f}%."
        )
    if not signals:
        print("No next-change signals meet the active tier.")
        return
    print(
        f"{'TIER':<12}{'STOCK':<7}{'CURRENT':>11}{'DIRECTION':>11}"
        f"{'EST PRICE':>12}{'CHANGE':>10}{'NET BPS':>10}{'STRENGTH':>11}"
    )
    for item in signals[:top]:
        print(
            f"{label:<12}{item['stock']:<7}{item['current_price']:>11.2f}"
            f"{item['direction']:>11}{item['estimated_price']:>12.2f}"
            f"{item['estimated_change']:>+10.2f}"
            f"{item['estimated_net_bps']:>10.1f}{item['strength']:>10.1f}x"
        )


def main():
    args=parse_args()
    try:
        cache_file=args.cache.expanduser().resolve()
        live_cache_file=args.live_cache.expanduser().resolve()
        forward_file=args.forward_file.expanduser().resolve()
        state=None
        base_cutoff=None
        if args.forward_test:
            state=load_forward_state(forward_file)
            base_cutoff=int(state["cutoff_timestamp"])
        elif (
            live_cache_file.is_file()
            and live_cache_file.stat().st_size
            and forward_file.is_file()
        ):
            base_cutoff=int(
                load_forward_state(forward_file)["cutoff_timestamp"]
            )
        data=load_runs(cache_file,live_cache_file,base_cutoff)
        if args.forward_test:
            result=evaluate_forward(data,state)
            if args.json:
                print(json.dumps(result,indent=2))
            else:
                print_forward_report(result)
            return(0)
        if args.freeze_forward:
            pipeline=build_change_pipeline(
                data,args.target_accuracy,args.sell_fee
            )
            state=create_forward_state(
                data,pipeline,cache_file,forward_file
            )
            if args.json:
                print(json.dumps(state,indent=2))
            else:
                print_forward_freeze(state,forward_file)
            return(0)
        structure=infer_structure(data)
        (
            split,hazards,ar2_coefficients,candidates,transitions,selected
        )=choose_models(
            data["states"],data["run_lengths"],args.validation,args.model
        )
        conditional_model=transitions[0]["model"]
        examples=event_examples(
            data,split,conditional_model,ar2_coefficients,args.event_samples
        )
        prediction=predict_latest(
            data,selected,hazards,ar2_coefficients,transitions,
            args.changed_only
        )
        pipeline=build_change_pipeline(
            data,args.target_accuracy,args.sell_fee
        )
        result=serializable_result(
            data,structure,candidates,transitions,selected,prediction,
            examples,args.changed_only
        )
        result["change_pipeline"]=pipeline
        if args.json:
            print(json.dumps(result,indent=2))
        else:
            print_report(result,args.top)
            print_change_pipeline(pipeline,args.top)
        return(0)
    except KeyboardInterrupt:
        print("\nInterrupted.",file=sys.stderr)
        return(130)
    except(
        OSError,ValueError,KeyError,TypeError,np.linalg.LinAlgError
    ) as error:
        print(f"Algorithm lab failed: {error}",file=sys.stderr)
        return(1)


if __name__=="__main__":
    raise SystemExit(main())
