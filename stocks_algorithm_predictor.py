import hashlib,json,sys
from dataclasses import asdict,dataclass
from pathlib import Path

import numpy as np

import stocks_algorithm_lab as lab


APP_DIR=(
    Path(sys.executable).resolve().parent
    if getattr(sys,"frozen",False)
    else Path(__file__).resolve().parent
)
DEFAULT_CACHE=APP_DIR/"stocks_cache.json"
DEFAULT_LIVE_CACHE=APP_DIR/"stocks_cache.jsonl"
DEFAULT_FORWARD=APP_DIR/"stocks_forward_test.json"
DEFAULT_PREDICTION=APP_DIR/"stocks_prediction.json"


@dataclass(frozen=True,slots=True)
class PredictionAccuracy:
    accuracy_percent:float
    accuracy_lower_bound_percent:float
    accuracy_source:str
    model_name:str
    validation_windows:int
    vector_accuracy_percent:float
    changed_price_accuracy_percent:float
    changed_direction_accuracy_percent:float
    all_stock_price_accuracy_percent:float
    all_stock_price_lower_bound_percent:float
    all_stock_sample_prices:int
    selective_price_accuracy_percent:float
    selective_price_lower_bound_percent:float
    historical_sample_actions:int
    historical_sample_events:int
    historical_profitable_percent:float
    historical_timing_percent:float
    historical_timing_lower_bound_percent:float
    forward_accuracy_percent:float|None
    forward_accuracy_lower_bound_percent:float|None
    forward_sample_actions:int
    target_accuracy_percent:float
    independently_confirmed:bool


@dataclass(frozen=True,slots=True)
class PredictionWindow:
    target_kind:str
    predicted_timestamp:int
    predicted_time:str
    earliest_timestamp:int
    earliest_time:str
    latest_timestamp:int
    latest_time:str
    exact_timestamp_known:bool
    exact_timestamp_accuracy_percent:float|None
    exact_timestamp_sample_events:int
    within_range_accuracy_percent:float
    within_range_lower_bound_percent:float
    horizon_windows:int


@dataclass(frozen=True,slots=True)
class StockPrediction:
    stock:str
    action:str
    trade_eligible:bool
    action_timestamp:int
    action_time:str
    target_action:str
    target_timestamp:int
    target_time:str
    current_price:float
    predicted_price:float
    predicted_change:float
    predicted_change_percent:float
    estimated_net_percent:float
    conditional_action:str
    conditional_predicted_price:float
    conditional_predicted_change:float
    conditional_predicted_change_percent:float
    prediction_accuracy_percent:float
    accuracy_lower_bound_percent:float
    accuracy_sample_actions:int
    strength:float


@dataclass(frozen=True,slots=True)
class StockAlgorithmPredictionResult:
    decision:str
    target_kind:str
    predicted_change_timestamp:int
    predicted_change_time:str
    model_name:str
    next_window_change_probability_percent:float
    selective_decision:str
    forward_status:str
    deployment_ready:bool
    experiment_id:str
    observed_through_timestamp:int
    observed_through_time:str
    next_evaluation_timestamp:int
    next_evaluation_time:str
    window:PredictionWindow|None
    accuracy:PredictionAccuracy
    predictions:tuple[StockPrediction,...]
    reasons:tuple[str,...]
    selective_reasons:tuple[str,...]

    def to_dict(self)->dict:
        return(asdict(self))


class StocksAlgorithmPredictor:
    def __init__(
        self,cache_file=None,live_cache_file=None,forward_file=None,
        prediction_file=None
    ):
        self.cache_file=Path(cache_file or DEFAULT_CACHE).expanduser().resolve()
        self.live_cache_file=Path(
            live_cache_file or DEFAULT_LIVE_CACHE
        ).expanduser().resolve()
        self.forward_file=Path(
            forward_file or DEFAULT_FORWARD
        ).expanduser().resolve()
        self.prediction_file=Path(
            prediction_file or DEFAULT_PREDICTION
        ).expanduser().resolve()

    def _save(self,result)->None:
        payload=result.to_dict()
        canonical=json.dumps(
            payload,sort_keys=True,separators=(",",":"),ensure_ascii=False
        ).encode("utf-8")
        payload["hash"]=hashlib.sha256(canonical).hexdigest()
        temporary=self.prediction_file.with_suffix(
            self.prediction_file.suffix+".tmp"
        )
        self.prediction_file.parent.mkdir(parents=True,exist_ok=True)
        temporary.write_text(
            json.dumps(payload,indent=2,ensure_ascii=False)+"\n",
            encoding="utf-8"
        )
        temporary.replace(self.prediction_file)

    def _load(self):
        state=lab.load_forward_state(self.forward_file)
        data=lab.load_runs(
            self.cache_file,self.live_cache_file,state["cutoff_timestamp"]
        )
        report=lab.evaluate_forward(data,state)
        (
            _,hazards,ar2_coefficients,_,transitions,selected
        )=lab.choose_models(
            data["states"],data["run_lengths"],.20,"auto"
        )
        transition=transitions[0]
        next_change=lab.predict_latest(
            data,selected,hazards,ar2_coefficients,transitions,True
        )
        return(state,data,report,transition,next_change)

    @staticmethod
    def _context_contiguous(data):
        if len(data["states"])<3:return(False)
        starts=data["state_starts"]
        ends=data["state_ends"]
        lengths=data["run_lengths"]
        internal=(
            ends[-3:]-starts[-3:]
            ==(lengths[-3:]-1)*lab.INTERVAL
        )
        boundaries=(
            starts[-2]-ends[-3]==lab.INTERVAL
            and starts[-1]-ends[-2]==lab.INTERVAL
        )
        return(bool(np.all(internal) and boundaries))

    @staticmethod
    def _historical_metrics(data,state):
        states=lab.cents(data["states"])
        changes=np.diff(states,axis=0)
        cutoff=int(state["cutoff_timestamp"])
        historical_count=int(np.sum(data["state_starts"][1:]<=cutoff))
        test_start=int(historical_count*.80)
        indexes=np.arange(max(2,test_start),historical_count)
        indexes,_=lab.clean_event_indexes(data,indexes)
        scale=np.asarray(state["scale_cents"],dtype=np.float64)
        block=lab.prepare_event_block(
            states,changes,indexes,scale,data["state_starts"],
            float(state["event_strong_z"])
        )
        price_state=state["price_tier"]
        maximum_prior_run=int(price_state["maximum_prior_run"])
        sell_fee=float(price_state["sell_fee"])
        horizon=int(price_state["horizon_windows"])
        price=lab.score_precision_trade(
            block,state["event_rule"],data["run_lengths"][indexes-2],
            maximum_prior_run,sell_fee
        )
        timing=lab.score_trade_horizon(
            block,state["event_rule"],data["run_lengths"][indexes-2],
            maximum_prior_run,data["run_lengths"][indexes],sell_fee,
            horizon
        )
        active,issued=lab.event_rule_masks(block,state["event_rule"])
        active&=data["run_lengths"][indexes-2]<=maximum_prior_run
        issued&=(
            active[:,None]
            &(np.abs(block["previous"])/block["current"]>=sell_fee)
        )
        event_rows=np.any(issued,axis=1)
        durations=data["run_lengths"][indexes][event_rows]
        event_count=int(np.sum(event_rows))
        total_prices=event_count*len(data["stock_ids"])
        exact_prices=int(np.sum(block["exact"][event_rows]))
        window_metrics={
            "exact_prices":exact_prices,
            "prices":total_prices,
            "accuracy_percent":exact_prices/max(1,total_prices)*100,
            "lower_percent":lab.wilson_lower(
                exact_prices,total_prices
            )*100
        }
        stock_metrics={}
        for column,stock in enumerate(data["stock_ids"]):
            exact=int(np.sum(block["exact"][event_rows,column]))
            stock_metrics[stock]={
                "exact_price_percent":exact/max(1,event_count)*100,
                "exact_price_wilson_lower_percent":lab.wilson_lower(
                    exact,event_count
                )*100,
                "actions":event_count
            }
        return(price,timing,durations,window_metrics,stock_metrics)

    @staticmethod
    def _accuracy(data,state,report,historical,timing,transition):
        forward=report["precision_trade_tier"]
        forward_actions=int(forward["issued_actions"])
        forward_accuracy=(
            float(forward["exact_price_percent"])
            if forward_actions else None
        )
        forward_lower=(
            float(forward["exact_price_wilson_lower_percent"])
            if forward_actions else None
        )
        confirmed=bool(report["deployment_ready"])
        windows=int(transition["windows"])
        total_prices=windows*len(data["stock_ids"])
        accuracy=float(transition["price_accuracy_percent"])
        correct=int(round(accuracy/100*total_prices))
        lower=lab.wilson_lower(correct,total_prices)*100
        return(PredictionAccuracy(
            accuracy_percent=accuracy,
            accuracy_lower_bound_percent=lower,
            accuracy_source="chronological_changed_state_validation",
            model_name=transition["name"],
            validation_windows=windows,
            vector_accuracy_percent=float(
                transition["vector_accuracy_percent"]
            ),
            changed_price_accuracy_percent=float(
                transition["changed_price_accuracy_percent"]
            ),
            changed_direction_accuracy_percent=float(
                transition["changed_direction_accuracy_percent"]
            ),
            all_stock_price_accuracy_percent=accuracy,
            all_stock_price_lower_bound_percent=lower,
            all_stock_sample_prices=total_prices,
            selective_price_accuracy_percent=float(
                historical["exact_price_percent"]
            ),
            selective_price_lower_bound_percent=float(
                historical["exact_price_wilson_lower_percent"]
            ),
            historical_sample_actions=int(historical["issued_actions"]),
            historical_sample_events=int(historical["events"]),
            historical_profitable_percent=float(
                historical["profitable_action_percent"]
            ),
            historical_timing_percent=float(
                timing["within_horizon_percent"]
            ),
            historical_timing_lower_bound_percent=float(
                timing["within_horizon_wilson_lower_percent"]
            ),
            forward_accuracy_percent=forward_accuracy,
            forward_accuracy_lower_bound_percent=forward_lower,
            forward_sample_actions=forward_actions,
            target_accuracy_percent=float(state["target_percent"]),
            independently_confirmed=confirmed
        ))

    @staticmethod
    def _window(data,next_change):
        interval=lab.INTERVAL
        maximum=int(data["maximum_timestamp"])
        predicted=int(next_change["estimated_change_timestamp"])
        horizon=max(1,(predicted-maximum)//interval)
        age=int(next_change["age"])
        completed=np.asarray(data["run_lengths"][:-1],dtype=np.int64)
        eligible=completed[completed>=age]
        predicted_length=age+horizon-1
        hits=int(np.sum(eligible==predicted_length))
        samples=len(eligible)
        timing_accuracy=hits/max(1,samples)*100
        timing_lower=lab.wilson_lower(hits,samples)*100
        return(PredictionWindow(
            target_kind="next_distinct_changed_state",
            predicted_timestamp=predicted,
            predicted_time=lab.forward_time(predicted),
            earliest_timestamp=predicted,
            earliest_time=lab.forward_time(predicted),
            latest_timestamp=predicted,
            latest_time=lab.forward_time(predicted),
            exact_timestamp_known=False,
            exact_timestamp_accuracy_percent=timing_accuracy,
            exact_timestamp_sample_events=samples,
            within_range_accuracy_percent=timing_accuracy,
            within_range_lower_bound_percent=timing_lower,
            horizon_windows=horizon
        ))

    @staticmethod
    def _predictions(
        data,state,next_window,window,accuracy
    ):
        current=np.asarray(next_window["current"],dtype=np.float64)
        predicted=np.asarray(next_window["primary"],dtype=np.float64)
        conditional=np.asarray(
            next_window["conditional"],dtype=np.float64
        )
        scale=np.asarray(state["scale_cents"],dtype=np.float64)
        strengths=np.abs((predicted-current)*100)/scale
        sell_fee=float(state["price_tier"]["sell_fee"])

        def classify(current_price,predicted_price):
            predicted_return=(predicted_price-current_price)/current_price
            if predicted_return>0:
                net=(1+predicted_return)*(1-sell_fee)-1
                return("BUY","SELL",net,net>0)
            if predicted_return<0:
                net=(1-sell_fee)/(1+predicted_return)-1
                return("SELL_OR_AVOID","BUY_BACK",net,net>0)
            return("HOLD","HOLD",0.0,False)

        predictions=[]
        for index,stock in enumerate(data["stock_ids"]):
            current_price=float(current[index])
            predicted_price=float(predicted[index])
            change=predicted_price-current_price
            change_percent=change/current_price*100
            name,target_action,net,eligible=classify(
                current_price,predicted_price
            )
            conditional_price=float(conditional[index])
            conditional_change=conditional_price-current_price
            conditional_action,_,_,_=classify(
                current_price,conditional_price
            )
            predictions.append(StockPrediction(
                stock=stock,
                action=name,
                trade_eligible=eligible,
                action_timestamp=int(data["maximum_timestamp"]),
                action_time=lab.forward_time(data["maximum_timestamp"]),
                target_action=target_action,
                target_timestamp=window.predicted_timestamp,
                target_time=window.predicted_time,
                current_price=current_price,
                predicted_price=predicted_price,
                predicted_change=change,
                predicted_change_percent=change_percent,
                estimated_net_percent=float(net*100),
                conditional_action=conditional_action,
                conditional_predicted_price=conditional_price,
                conditional_predicted_change=conditional_change,
                conditional_predicted_change_percent=(
                    conditional_change/current_price*100
                ),
                prediction_accuracy_percent=float(
                    accuracy.accuracy_percent
                ),
                accuracy_lower_bound_percent=float(
                    accuracy.accuracy_lower_bound_percent
                ),
                accuracy_sample_actions=int(
                    accuracy.all_stock_sample_prices
                ),
                strength=float(strengths[index])
            ))
        return(tuple(predictions))

    def predict(self,save:bool=False)->StockAlgorithmPredictionResult:
        state,data,report,transition,next_change=self._load()
        (
            historical,timing,_,_,_
        )=self._historical_metrics(data,state)
        accuracy=self._accuracy(
            data,state,report,historical,timing,transition
        )
        selective=dict(report["current_prediction"])
        selective_reasons=list(selective.get("reasons",[]))
        if (
            selective["decision"]=="PREDICT"
            and not self._context_contiguous(data)
        ):
            selective["decision"]="ABSTAIN"
            selective_reasons.append(
                "latest three-state context is not grid-contiguous"
            )
        window=self._window(data,next_change)
        predictions=self._predictions(
            data,state,next_change,window,accuracy
        )
        observed=int(data["maximum_timestamp"])
        next_evaluation=observed+lab.INTERVAL
        reasons=[]
        if selective["decision"]!="PREDICT":
            reasons.append(
                "selective high-confidence tier abstained; returning the "
                "always-on changed-state forecast"
            )
        result=StockAlgorithmPredictionResult(
            decision="PREDICT",
            target_kind=window.target_kind,
            predicted_change_timestamp=window.predicted_timestamp,
            predicted_change_time=window.predicted_time,
            model_name=transition["model"],
            next_window_change_probability_percent=float(
                next_change["change_probability_percent"]
            ),
            selective_decision=selective["decision"],
            forward_status=report["status"],
            deployment_ready=bool(report["deployment_ready"]),
            experiment_id=state["experiment_id"],
            observed_through_timestamp=observed,
            observed_through_time=lab.forward_time(observed),
            next_evaluation_timestamp=next_evaluation,
            next_evaluation_time=lab.forward_time(next_evaluation),
            window=window,
            accuracy=accuracy,
            predictions=predictions,
            reasons=tuple(reasons),
            selective_reasons=tuple(selective_reasons)
        )
        if save:self._save(result)
        return(result)

    def get_predictions(
        self,save:bool=False
    )->StockAlgorithmPredictionResult:
        return(self.predict(save))
if __name__=="__main__":
    result=StocksAlgorithmPredictor().predict(save=True)
    print(json.dumps(result.to_dict(),indent=2))