#!/usr/bin/env python3
"""Generate offline daily Tick/1m/level HTML reports and Chrome screenshots."""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


VISUAL_VERSION = "1.0.0"
CHROME_CANDIDATES = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
]


def read_csv(path: Path) -> List[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_ticks(path: Path) -> List[dict]:
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def parse_event_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")


def minute_key(value: str) -> str:
    return value[:16]


def aggregate_1m(ticks: Sequence[Mapping[str, str]]) -> List[dict]:
    bars: List[dict] = []
    current: Optional[dict] = None
    for row in ticks:
        key = minute_key(row["event_time"])
        price = float(row["last_price"])
        if current is None or current["minute"] != key:
            if current is not None:
                bars.append(current)
            current = {
                "minute": key,
                "time": key + ":00",
                "session": row["session"],
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": float(row["delta_volume"]),
                "open_interest": float(row["open_interest"]),
                "tick_count": 1,
            }
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += float(row["delta_volume"])
            current["open_interest"] = float(row["open_interest"])
            current["tick_count"] += 1
    if current is not None:
        bars.append(current)
    return bars


def bar_position_map(bars: Sequence[Mapping[str, object]]) -> Dict[str, int]:
    return {str(bar["minute"]): index for index, bar in enumerate(bars)}


def tick_x(row: Mapping[str, str], positions: Mapping[str, int]) -> Optional[float]:
    key = minute_key(row["event_time"])
    if key not in positions:
        return None
    timestamp = parse_event_time(row["event_time"])
    return positions[key] + (timestamp.second + timestamp.microsecond / 1_000_000) / 60.0


def relevant_ticks(ticks: Sequence[Mapping[str, str]]) -> List[Mapping[str, str]]:
    result = []
    previous_price: Optional[float] = None
    for row in ticks:
        price = float(row["last_price"])
        delta_volume = float(row["delta_volume"])
        if previous_price is None or price != previous_price or delta_volume > 0:
            result.append(row)
        previous_price = price
    return result


def lod_ticks(ticks: Sequence[Mapping[str, str]], bucket_millis: int = 1000) -> List[Mapping[str, str]]:
    """Preserve first/min/max/last observation in each time bucket."""
    buckets: Dict[int, List[Tuple[int, Mapping[str, str]]]] = defaultdict(list)
    for index, row in enumerate(ticks):
        bucket = int(row["millis_of_day"]) // bucket_millis
        buckets[bucket].append((index, row))
    selected: List[Tuple[int, Mapping[str, str]]] = []
    for bucket in sorted(buckets):
        group = buckets[bucket]
        candidates = [group[0], group[-1]]
        candidates.append(min(group, key=lambda item: float(item[1]["last_price"])))
        candidates.append(max(group, key=lambda item: float(item[1]["last_price"])))
        by_index = {index: row for index, row in candidates}
        selected.extend(sorted(by_index.items()))
    return [row for _, row in sorted(selected)]


def detect_breaks(bars: Sequence[Mapping[str, object]]) -> List[dict]:
    result = []
    for index in range(1, len(bars)):
        previous = datetime.strptime(str(bars[index - 1]["time"]), "%Y-%m-%d %H:%M:%S")
        current = datetime.strptime(str(bars[index]["time"]), "%Y-%m-%d %H:%M:%S")
        gap_minutes = (current - previous).total_seconds() / 60
        if gap_minutes > 1.5:
            label = "午休" if gap_minutes >= 60 else "休盘"
            result.append({"x": index - 0.5, "label": label, "minutes": gap_minutes - 1})
    return result


def first_afternoon_x(bars: Sequence[Mapping[str, object]]) -> float:
    for index, bar in enumerate(bars):
        if bar["session"] == "afternoon":
            return max(0.0, index - 0.5)
    return float(len(bars) - 1)


def event_key(row: Mapping[str, str]) -> Tuple[str, str, str, str]:
    return (row["node_types"], row["center"], row["lower"], row["upper"])


def event_x(row: Mapping[str, str], positions: Mapping[str, int]) -> Optional[float]:
    key = minute_key(row["touch_time"])
    if key not in positions:
        return None
    timestamp = parse_event_time(row["touch_time"])
    return positions[key] + (timestamp.second + timestamp.microsecond / 1_000_000) / 60.0


def compact_number(value: float) -> float:
    return round(value, 10)


def build_payload(
    instrument: str,
    selection: Mapping[str, str],
    ticks: Sequence[Mapping[str, str]],
    profiles: Sequence[Mapping[str, str]],
    levels: Sequence[Mapping[str, str]],
    events: Sequence[Mapping[str, str]],
    config: Mapping[str, object],
) -> dict:
    bars = aggregate_1m(ticks)
    positions = bar_position_map(bars)
    reduced = relevant_ticks(ticks)
    overview = lod_ticks(reduced)
    tick_full = []
    for row in reduced:
        x = tick_x(row, positions)
        if x is not None:
            tick_full.append([
                compact_number(x), float(row["last_price"]), row["event_time"],
                float(row["delta_volume"]), row["quality_flags"],
            ])
    tick_overview = []
    for row in overview:
        x = tick_x(row, positions)
        if x is not None:
            tick_overview.append([
                compact_number(x), float(row["last_price"]), row["event_time"],
                float(row["delta_volume"]), row["quality_flags"],
            ])
    event_by_level = {event_key(row): row for row in events}
    start_x = first_afternoon_x(bars)
    level_data = []
    for row in levels:
        event = event_by_level.get(event_key(row))
        level_data.append({
            "type": row["node_types"],
            "center": float(row["center"]),
            "lower": float(row["lower"]),
            "upper": float(row["upper"]),
            "available": row["available_time"],
            "startX": start_x,
            "source": row["source"],
            "persistence": int(float(row["scale_persistence"])),
            "prominence": float(row["prominence_share"]),
            "role": event["role"] if event else "neutral",
            "outcome": event["outcome"] if event else "no_touch",
        })
    event_data = []
    for row in events:
        x = event_x(row, positions)
        if x is not None:
            event_data.append({
                "x": compact_number(x), "price": float(row["touch_price"]),
                "time": row["touch_time"], "type": row["node_types"],
                "role": row["role"], "outcome": row["outcome"],
                "mfe": float(row["mfe_ticks"]), "mae": float(row["mae_ticks"]),
            })
    profile_data: Dict[str, Dict[str, List[List[float]]]] = defaultdict(dict)
    for row in profiles:
        profile_data[row["method"]].setdefault(row["sigma_ticks"], []).append([
            float(row["price"]), float(row["volume"]),
        ])
    root = selection["root"]
    meta = config["contracts"][root]  # type: ignore[index]
    values = [float(bar[key]) for bar in bars for key in ("low", "high")]
    values.extend(float(row["lower"]) for row in levels)
    values.extend(float(row["upper"]) for row in levels)
    price_min, price_max = min(values), max(values)
    padding = max(float(meta["tick_size"]) * 2, (price_max - price_min) * 0.025)
    return {
        "version": VISUAL_VERSION,
        "tradingDay": ticks[0]["trading_day"],
        "instrument": instrument,
        "root": root,
        "exchange": selection["exchange"],
        "tickSize": float(meta["tick_size"]),
        "availableTime": levels[0]["available_time"] if levels else "",
        "bars": [[
            bar["time"], bar["open"], bar["close"], bar["low"], bar["high"],
            bar["volume"], bar["open_interest"], bar["session"], bar["tick_count"],
        ] for bar in bars],
        "ticksOverview": tick_overview,
        "ticksFull": tick_full,
        "breaks": detect_breaks(bars),
        "levels": level_data,
        "events": event_data,
        "profiles": profile_data,
        "priceMin": price_min - padding,
        "priceMax": price_max + padding,
        "stats": {
            "ticks": len(ticks), "tickPoints": len(tick_full), "bars": len(bars),
            "levels": len(level_data), "events": len(event_data),
            "bounce": sum(row["outcome"] == "bounce" for row in events),
            "break": sum(row["outcome"] == "break" for row in events),
            "timeout": sum(row["outcome"] == "timeout" for row in events),
        },
    }


def safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


DETAIL_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light dark;--bg:#ffffff;--fg:#1d2939;--muted:#667085;--surface:#f7f8fa;--border:#d9dde5;--up:#d9544d;--down:#218a63;--support:#14866d;--resistance:#c94b3c;--neutral:#b57418;--primary:#285fa8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}.shell{min-height:100vh;display:grid;grid-template-rows:auto minmax(620px,1fr);padding:14px 16px;gap:10px}.topbar{display:flex;align-items:center;gap:10px;min-width:0}.identity{min-width:210px}.identity h1{font-size:18px;margin:0;font-weight:650;letter-spacing:0}.identity span{color:var(--muted)}.metrics{display:flex;gap:16px;color:var(--muted);white-space:nowrap}.metrics b{color:var(--fg);font-weight:600}.controls{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}button,select{height:32px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);padding:0 10px;font:inherit}button:hover{background:var(--surface)}button.active{background:var(--primary);color:#fff;border-color:var(--primary)}.layout{min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 248px;border:1px solid var(--border);border-radius:8px;overflow:hidden}.chart{min-width:0;min-height:620px}.side{border-left:1px solid var(--border);background:var(--surface);overflow:auto;padding:10px}.side h2{font-size:13px;margin:2px 0 8px}.level{padding:8px 0;border-bottom:1px solid var(--border)}.levelhead{display:flex;align-items:center;justify-content:space-between;gap:8px}.badge{display:inline-flex;align-items:center;gap:5px}.dot{width:8px;height:8px;border-radius:50%;background:var(--neutral)}.dot.support{background:var(--support)}.dot.resistance{background:var(--resistance)}.zone{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--fg)}.sub{color:var(--muted);margin-top:3px;font-size:12px}.warning{border-left:3px solid var(--neutral);padding:7px 8px;margin-bottom:8px;color:var(--muted);background:var(--bg)}@media(prefers-color-scheme:dark){:root{--bg:#111318;--fg:#e5e7eb;--muted:#9ca3af;--surface:#181b22;--border:#333844;--up:#e77770;--down:#4db38b;--support:#4db39a;--resistance:#e0786b;--neutral:#d59a44;--primary:#5f93d6}}@media(max-width:900px){.shell{padding:8px}.topbar{align-items:flex-start;flex-wrap:wrap}.metrics{order:3;width:100%;overflow:auto}.controls{margin-left:0}.layout{grid-template-columns:minmax(0,1fr)}.side{border-left:0;border-top:1px solid var(--border);max-height:240px}.chart{min-height:560px}}
</style>
</head>
<body>
<div class="shell">
  <div class="topbar">
    <div class="identity"><h1 id="title"></h1><span id="subtitle"></span></div>
    <div class="metrics" id="metrics"></div>
    <div class="controls">
      <button id="tickToggle" class="active">Tick</button>
      <button id="zoneToggle" class="active">Zones</button>
      <select id="profileMethod"><option>LTP</option><option>VWAP</option></select>
      <select id="profileSigma"><option value="0">Raw</option><option value="1">σ 1</option><option value="2" selected>σ 2</option><option value="4">σ 4</option></select>
      <button id="pngExport">导出 PNG</button>
      <button id="svgExport">导出 SVG</button>
    </div>
  </div>
  <div class="layout">
    <div id="chart" class="chart"></div>
    <aside class="side"><div class="warning">单日 smoke test，仅展示 point-in-time level 与价格路径。</div><h2>Level zones</h2><div id="levels"></div></aside>
  </div>
</div>
<script src="__ECHARTS_ASSET__"></script>
<script>const DATA=__PAYLOAD__;</script>
<script>
const dark=matchMedia('(prefers-color-scheme: dark)').matches;
const C=dark?{fg:'#e5e7eb',muted:'#9ca3af',grid:'#333844',up:'#e77770',down:'#4db38b',support:'#4db39a',resistance:'#e0786b',neutral:'#d59a44',primary:'#5f93d6'}:{fg:'#1d2939',muted:'#667085',grid:'#e3e6eb',up:'#d9544d',down:'#218a63',support:'#14866d',resistance:'#c94b3c',neutral:'#b57418',primary:'#285fa8'};
const chart=echarts.init(document.getElementById('chart'),null,{renderer:'canvas'});
let showTick=true,showZones=true,detailedTick=false,method='LTP',sigma='2';
const bars=DATA.bars,maxX=Math.max(1,bars.length-1);
const barTime=v=>{const i=Math.max(0,Math.min(bars.length-1,Math.round(v)));return bars[i][0].slice(11,16)};
const levelColor=z=>z.role==='support'?C.support:z.role==='resistance'?C.resistance:C.neutral;
const roleLabel=r=>r==='support'?'support':r==='resistance'?'resistance':'neutral';
function candleRender(params,api){const x=api.coord([api.value(0),api.value(1)])[0],yo=api.coord([0,api.value(1)])[1],yc=api.coord([0,api.value(2)])[1],yl=api.coord([0,api.value(3)])[1],yh=api.coord([0,api.value(4)])[1],w=Math.max(2,Math.min(9,api.size([1,0])[0]*.58)),up=api.value(2)>=api.value(1),color=up?C.up:C.down;return{type:'group',children:[{type:'line',shape:{x1:x,y1:yh,x2:x,y2:yl},style:{stroke:color,lineWidth:1}},{type:'rect',shape:{x:x-w/2,y:Math.min(yo,yc),width:w,height:Math.max(1,Math.abs(yc-yo))},style:{fill:color,stroke:color}}]}}
function zoneRender(params,api){const z=DATA.levels[params.dataIndex],p0=api.coord([z.startX,z.upper]),p1=api.coord([maxX,z.lower]),rect=echarts.graphic.clipRectByRect({x:p0[0],y:p0[1],width:p1[0]-p0[0],height:p1[1]-p0[1]},{x:params.coordSys.x,y:params.coordSys.y,width:params.coordSys.width,height:params.coordSys.height});if(!rect)return null;const color=levelColor(z);return{type:'group',children:[{type:'rect',shape:rect,style:{fill:color,opacity:.12,stroke:color,lineWidth:1,lineDash:z.role==='neutral'?[5,4]:null}},{type:'text',style:{x:params.coordSys.x+params.coordSys.width-4,y:(p0[1]+p1[1])/2,text:z.type,fill:color,font:'11px sans-serif',textAlign:'right',textVerticalAlign:'middle'}}]}}
function profileBarRender(params,api){const price=api.value(0),volume=api.value(1),p0=api.coord([0,price]),p1=api.coord([volume,price]),h=Math.max(1,Math.min(7,Math.abs(api.size([0,DATA.tickSize])[1])*.72));return{type:'rect',shape:{x:p0[0],y:p0[1]-h/2,width:Math.max(0,p1[0]-p0[0]),height:h},style:{fill:C.muted,opacity:.28}}}
function profileSeries(){const raw=DATA.profiles[method]['0']||[],smooth=DATA.profiles[method][sigma]||raw;return[{id:'profileBars',name:method+' raw',type:'custom',xAxisIndex:1,yAxisIndex:1,encode:{x:1,y:0},renderItem:profileBarRender,data:raw,silent:true,z:1},{id:'profileLine',name:method+' σ'+sigma,type:'line',xAxisIndex:1,yAxisIndex:1,data:smooth.map(d=>[d[1],d[0]]),showSymbol:false,smooth:.24,lineStyle:{color:C.primary,width:1.4},silent:true,z:3}]}
const eventData=DATA.events.map(e=>({value:[e.x,e.price,e.time,e.type,e.role,e.outcome,e.mfe,e.mae],symbol:e.outcome==='bounce'?'circle':e.outcome==='break'?'diamond':'triangle',symbolSize:9,itemStyle:{color:e.outcome==='bounce'?C.support:e.outcome==='break'?C.resistance:C.neutral}}));
function baseOption(){return{animation:false,backgroundColor:'transparent',textStyle:{color:C.fg,fontFamily:'-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif'},grid:[{left:54,right:184,top:18,height:'62%'},{right:18,width:146,top:18,height:'62%'},{left:54,right:184,top:'72%',bottom:38}],xAxis:[{type:'value',min:-.7,max:maxX+.7,gridIndex:0,axisLabel:{color:C.muted,formatter:barTime},axisLine:{lineStyle:{color:C.grid}},splitLine:{show:true,lineStyle:{color:C.grid}}},{type:'value',gridIndex:1,axisLabel:{show:false},axisLine:{show:false},splitLine:{show:false}},{type:'value',min:-.7,max:maxX+.7,gridIndex:2,axisLabel:{color:C.muted,formatter:barTime},axisLine:{lineStyle:{color:C.grid}},splitLine:{show:false}}],yAxis:[{type:'value',min:DATA.priceMin,max:DATA.priceMax,scale:true,gridIndex:0,axisLabel:{color:C.muted},axisLine:{show:false},splitLine:{show:true,lineStyle:{color:C.grid}}},{type:'value',min:DATA.priceMin,max:DATA.priceMax,scale:true,gridIndex:1,position:'right',axisLabel:{show:false},axisLine:{show:false},splitLine:{show:false}},{type:'value',gridIndex:2,axisLabel:{color:C.muted},axisLine:{show:false},splitLine:{show:true,lineStyle:{color:C.grid}}},{type:'value',gridIndex:2,position:'right',axisLabel:{show:false},axisLine:{show:false},splitLine:{show:false}}],dataZoom:[{type:'inside',xAxisIndex:[0,2],filterMode:'none',zoomOnMouseWheel:true,moveOnMouseMove:true},{type:'slider',xAxisIndex:[0,2],bottom:4,height:18,borderColor:C.grid,fillerColor:dark?'rgba(95,147,214,.18)':'rgba(40,95,168,.14)',handleStyle:{color:C.primary}}],axisPointer:{link:[{xAxisIndex:[0,2]}]},tooltip:{trigger:'axis',confine:true,backgroundColor:dark?'#181b22':'#fff',borderColor:C.grid,textStyle:{color:C.fg},formatter:params=>{const p=params.find(x=>x.seriesId==='tick')||params[0];if(!p)return'';if(p.seriesId==='tick')return p.data[2]+'<br>Tick '+p.data[1]+'<br>ΔV '+p.data[3];const i=Math.max(0,Math.min(bars.length-1,Math.round(p.value[0]))),b=bars[i];return b[0]+'<br>O '+b[1]+' H '+b[4]+' L '+b[3]+' C '+b[2]+'<br>Volume '+Math.round(b[5])}},series:[{id:'zones',name:'Zones',type:'custom',xAxisIndex:0,yAxisIndex:0,renderItem:zoneRender,data:DATA.levels.map(z=>[z.startX,z.lower,z.upper]),silent:true,z:0},{id:'candles',name:'1min Kline',type:'custom',xAxisIndex:0,yAxisIndex:0,renderItem:candleRender,data:bars.map((b,i)=>[i,b[1],b[2],b[3],b[4],b[0]]),z:3},{id:'tick',name:'Tick path',type:'line',xAxisIndex:0,yAxisIndex:0,data:DATA.ticksOverview,showSymbol:false,sampling:'lttb',progressive:4000,lineStyle:{color:C.muted,width:.8,opacity:.55},z:4},{id:'events',name:'Events',type:'scatter',xAxisIndex:0,yAxisIndex:0,data:eventData,z:7,tooltip:{formatter:p=>p.data.value[2]+'<br>'+p.data.value[3]+' · '+p.data.value[4]+' · '+p.data.value[5]+'<br>MFE '+p.data.value[6]+' / MAE '+p.data.value[7]}},{id:'breaks',name:'休盘',type:'line',xAxisIndex:0,yAxisIndex:0,data:[],silent:true,markLine:{silent:true,symbol:['none','none'],lineStyle:{color:C.neutral,type:'dashed',width:1},label:{color:C.neutral,formatter:p=>p.name},data:DATA.breaks.map(b=>({name:b.label,xAxis:b.x}))}},{id:'volume',name:'Volume',type:'bar',xAxisIndex:2,yAxisIndex:2,data:bars.map((b,i)=>[i,b[5]]),barWidth:'52%',itemStyle:{color:C.primary,opacity:.46},large:true},{id:'oi',name:'Open interest',type:'line',xAxisIndex:2,yAxisIndex:3,data:bars.map((b,i)=>[i,b[6]]),showSymbol:false,lineStyle:{color:C.neutral,width:1},silent:true},...profileSeries()]}}
let option=baseOption();chart.setOption(option);
function updateProfile(){chart.setOption({series:profileSeries()})}
function updateTick(){chart.setOption({series:[{id:'tick',data:showTick?(detailedTick?DATA.ticksFull:DATA.ticksOverview):[]}]})}
function updateZones(){chart.setOption({series:[{id:'zones',data:showZones?DATA.levels.map(z=>[z.startX,z.lower,z.upper]):[]}]})}
chart.on('datazoom',()=>{const opt=chart.getOption().dataZoom||[],inside=opt[0]||{},span=(inside.end??100)-(inside.start??0),next=span<35;if(next!==detailedTick){detailedTick=next;updateTick()}});
document.getElementById('profileMethod').onchange=e=>{method=e.target.value;updateProfile()};document.getElementById('profileSigma').onchange=e=>{sigma=e.target.value;updateProfile()};
const tickBtn=document.getElementById('tickToggle');tickBtn.onclick=()=>{showTick=!showTick;tickBtn.classList.toggle('active',showTick);updateTick()};const zoneBtn=document.getElementById('zoneToggle');zoneBtn.onclick=()=>{showZones=!showZones;zoneBtn.classList.toggle('active',showZones);updateZones()};
function download(data,name){const a=document.createElement('a');a.href=data;a.download=name;a.click()}
document.getElementById('pngExport').onclick=()=>download(chart.getDataURL({type:'png',pixelRatio:2,backgroundColor:dark?'#111318':'#fff'}),DATA.instrument+'_'+DATA.tradingDay+'.png');
document.getElementById('svgExport').onclick=()=>{const d=document.createElement('div');d.style.cssText='width:1600px;height:900px;position:absolute;left:-99999px;top:0';document.body.appendChild(d);const c=echarts.init(d,null,{renderer:'svg'});c.setOption(baseOption());setTimeout(()=>{const blob=new Blob([d.innerHTML],{type:'image/svg+xml'});download(URL.createObjectURL(blob),DATA.instrument+'_'+DATA.tradingDay+'.svg');c.dispose();d.remove()},120)};
window.onresize=()=>chart.resize();
document.getElementById('title').textContent=DATA.instrument+' · '+DATA.tradingDay;document.getElementById('subtitle').textContent=DATA.exchange+' · Tick '+DATA.tickSize+' · available '+DATA.availableTime.slice(11);
document.getElementById('metrics').innerHTML='<span>Ticks <b>'+DATA.stats.ticks.toLocaleString()+'</b></span><span>1min <b>'+DATA.stats.bars+'</b></span><span>Zones <b>'+DATA.stats.levels+'</b></span><span>Events <b>'+DATA.stats.events+'</b></span>';
document.getElementById('levels').innerHTML=DATA.levels.map(z=>'<div class="level"><div class="levelhead"><span class="badge"><span class="dot '+z.role+'"></span>'+z.type+'</span><span class="zone">'+z.lower+'–'+z.upper+'</span></div><div class="sub">'+roleLabel(z.role)+' · '+z.outcome+' · persistence '+z.persistence+'</div></div>').join('');
window.__REPORT_READY__=true;
</script>
</body></html>'''


INDEX_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>:root{color-scheme:light dark;--bg:#fff;--fg:#1d2939;--muted:#667085;--surface:#f7f8fa;--border:#d9dde5;--primary:#285fa8;--bounce:#14866d;--break:#c94b3c}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}.page{max-width:1500px;margin:auto;padding:20px}.head{display:flex;gap:16px;align-items:end;margin-bottom:16px}.head h1{margin:0;font-size:22px}.head p{margin:4px 0 0;color:var(--muted)}input{margin-left:auto;width:240px;height:34px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);padding:0 10px}.warning{border-left:3px solid #b57418;background:var(--surface);color:var(--muted);padding:9px 12px;margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:12px}.item{display:block;color:inherit;text-decoration:none;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--bg)}.item:hover{border-color:var(--primary)}.meta{display:flex;justify-content:space-between;gap:8px;padding:10px 12px 4px}.name{font-weight:650}.counts{color:var(--muted)}.chart{height:132px;padding:0 8px}.foot{display:flex;gap:12px;padding:6px 12px 10px;color:var(--muted)}.foot b{font-weight:600}.bounce{color:var(--bounce)}.break{color:var(--break)}svg{width:100%;height:100%}@media(prefers-color-scheme:dark){:root{--bg:#111318;--fg:#e5e7eb;--muted:#9ca3af;--surface:#181b22;--border:#333844;--primary:#5f93d6;--bounce:#4db39a;--break:#e0786b}}@media(max-width:700px){.page{padding:10px}.head{align-items:flex-start;flex-wrap:wrap}.head input{margin-left:0;width:100%}}</style></head>
<body><main class="page"><div class="head"><div><h1>__HEADING__</h1><p>1min K线、上午冻结 zones 与下午 first-touch 结果</p></div><input id="search" placeholder="筛选合约"></div><div class="warning">单日 smoke test，不代表统计显著或可交易收益。</div><div class="grid" id="grid">__CARDS__</div></main><script>const q=document.getElementById('search');q.oninput=()=>{const v=q.value.trim().toLowerCase();document.querySelectorAll('.item').forEach(x=>x.style.display=x.dataset.name.includes(v)?'block':'none')}</script></body></html>'''


def mini_svg(payload: Mapping[str, object]) -> str:
    bars = payload["bars"]  # type: ignore[index]
    levels = payload["levels"]  # type: ignore[index]
    width, height, left, top, bottom = 320, 126, 5, 5, 10
    prices = [float(bar[index]) for bar in bars for index in (3, 4)]
    low, high = min(prices), max(prices)
    span = max(1e-9, high - low)

    def x(index: int) -> float:
        return left + index * (width - left * 2) / max(1, len(bars) - 1)

    def y(price: float) -> float:
        return top + (high - price) * (height - top - bottom) / span

    pieces = []
    for level in levels:
        start = float(level["startX"])
        ly = y(float(level["center"]))
        color = "#14866d" if level["role"] == "support" else "#c94b3c" if level["role"] == "resistance" else "#b57418"
        pieces.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="1" stroke-dasharray="4 3" opacity=".75"/>' % (x(int(start)), ly, width - left, ly, color))
    for index, bar in enumerate(bars):
        px = x(index)
        open_price, close, lo, hi = (float(bar[i]) for i in (1, 2, 3, 4))
        color = "#d9544d" if close >= open_price else "#218a63"
        pieces.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="1"/>' % (px, y(hi), px, y(lo), color))
        pieces.append('<rect x="%.2f" y="%.2f" width="2" height="%.2f" fill="%s"/>' % (px - 1, min(y(open_price), y(close)), max(1, abs(y(open_price) - y(close))), color))
    return '<svg viewBox="0 0 %d %d" preserveAspectRatio="none" aria-label="%s">%s</svg>' % (width, height, html.escape(str(payload["instrument"])), "".join(pieces))


def render_detail(payload: Mapping[str, object], asset_path: str) -> str:
    title = "%s %s 日内行情" % (payload["instrument"], payload["tradingDay"])
    return DETAIL_TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__ECHARTS_ASSET__", asset_path).replace("__PAYLOAD__", safe_json(payload))


def render_index(payloads: Sequence[Mapping[str, object]]) -> str:
    cards = []
    for payload in payloads:
        filename = "%s_%s.html" % (payload["instrument"], payload["tradingDay"])
        stats = payload["stats"]
        cards.append(
            '<a class="item" data-name="%s" href="details/%s"><div class="meta"><span class="name">%s</span><span class="counts">%s · %s bars</span></div><div class="chart">%s</div><div class="foot"><span>Zones <b>%s</b></span><span class="bounce">Bounce <b>%s</b></span><span class="break">Break <b>%s</b></span></div></a>' % (
                html.escape(str(payload["instrument"]).lower()), html.escape(filename), html.escape(str(payload["instrument"])),
                html.escape(str(payload["exchange"])), stats["bars"], mini_svg(payload), stats["levels"], stats["bounce"], stats["break"],
            )
        )
    day = str(payloads[0]["tradingDay"]) if payloads else ""
    heading = "%s 日内行情总览" % day
    return INDEX_TEMPLATE.replace("__TITLE__", html.escape(heading)).replace("__HEADING__", html.escape(heading)).replace("__CARDS__", "".join(cards))


def find_chrome(explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit:
        return explicit if explicit.exists() else None
    return next((path for path in CHROME_CANDIDATES if path.exists()), None)


def screenshot(chrome: Path, source: Path, target: Path, width: int, height: int, profile_root: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    profile_dir = profile_root / target.stem
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--allow-file-access-from-files", "--no-first-run",
        "--disable-background-networking", "--disable-extensions",
        "--run-all-compositor-stages-before-draw", "--virtual-time-budget=2500",
        "--user-data-dir=%s" % profile_dir,
        "--window-size=%d,%d" % (width, height),
        "--screenshot=%s" % target,
        source.resolve().as_uri(),
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    stderr = ""
    last_size = -1
    stable_polls = 0
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline and process.poll() is None:
            size = target.stat().st_size if target.exists() else 0
            if size > 1024 and size == last_size:
                stable_polls += 1
                if stable_polls >= 3:
                    break
            else:
                stable_polls = 0
            last_size = size
            time.sleep(0.2)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            _, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            _, stderr = process.communicate()
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    if not target.exists() or target.stat().st_size < 1024:
        raise RuntimeError("Chrome screenshot failed for %s: %s" % (source, stderr[-1000:]))


def generate_visuals(
    data_dir: Path,
    output_dir: Path,
    config_path: Path,
    echarts_asset: Path,
    chrome: Optional[Path],
    images: bool,
    overwrite: bool,
) -> dict:
    if output_dir.exists() and any(output_dir.iterdir()):
        if not overwrite:
            raise FileExistsError("visual output is not empty; use --overwrite: %s" % output_dir)
        shutil.rmtree(output_dir)
    details_dir = output_dir / "details"
    assets_dir = output_dir / "assets"
    images_dir = output_dir / "images"
    details_dir.mkdir(parents=True)
    assets_dir.mkdir()
    images_dir.mkdir()
    shutil.copy2(echarts_asset, assets_dir / "echarts.min.js")
    license_path = echarts_asset.with_name("ECHARTS-LICENSE.txt")
    if license_path.exists():
        shutil.copy2(license_path, assets_dir / license_path.name)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    selections = {row["instrument"]: row for row in read_csv(data_dir / "selected_contracts.csv") if row["selected"] == "true"}
    profile_rows = read_csv(data_dir / "profiles.csv")
    level_rows = read_csv(data_dir / "levels.csv")
    event_rows = read_csv(data_dir / "events.csv")
    profiles_by_instrument: Dict[str, List[dict]] = defaultdict(list)
    levels_by_instrument: Dict[str, List[dict]] = defaultdict(list)
    events_by_instrument: Dict[str, List[dict]] = defaultdict(list)
    for row in profile_rows:
        profiles_by_instrument[row["instrument"]].append(row)
    for row in level_rows:
        levels_by_instrument[row["instrument"]].append(row)
    for row in event_rows:
        events_by_instrument[row["instrument"]].append(row)

    payloads = []
    manifest_items = []
    for instrument in sorted(selections):
        selection = selections[instrument]
        tick_path = data_dir / "selected_ticks" / (Path(selection["source_file"]).stem + ".csv.gz")
        ticks = read_ticks(tick_path)
        payload = build_payload(
            instrument, selection, ticks, profiles_by_instrument[instrument],
            levels_by_instrument[instrument], events_by_instrument[instrument], config,
        )
        payloads.append(payload)
        filename = "%s_%s.html" % (instrument, payload["tradingDay"])
        detail_path = details_dir / filename
        detail_path.write_text(render_detail(payload, "../assets/echarts.min.js"), encoding="utf-8")
        manifest_items.append({
            "instrument": instrument, "root": payload["root"], "exchange": payload["exchange"],
            "trading_day": payload["tradingDay"], "tick_size": payload["tickSize"],
            "available_time": payload["availableTime"], "detail": "details/" + filename,
            "image": "images/%s_%s.png" % (instrument, payload["tradingDay"]),
            "stats": payload["stats"],
        })
    (output_dir / "index.html").write_text(render_index(payloads), encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "visual_version": VISUAL_VERSION,
        "trading_day": payloads[0]["tradingDay"] if payloads else "",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "renderer": "Apache ECharts 5.6.0",
        "profile_methods": ["LTP", "VWAP"],
        "smoothing_sigmas_ticks": config["profile"]["smoothing_sigmas_ticks"],
        "instruments": manifest_items,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    screenshot_count = 0
    if images:
        if chrome is None:
            raise RuntimeError("Chrome was not found; rerun with --skip-images or --chrome")
        profile_dir = output_dir / ".chrome-profile"
        profile_dir.mkdir()
        try:
            for item in manifest_items:
                screenshot(chrome, output_dir / item["detail"], output_dir / item["image"], 1920, 1080, profile_dir)
                screenshot_count += 1
            overview_height = max(1080, 220 + math.ceil(len(manifest_items) / 4) * 240)
            screenshot(chrome, output_dir / "index.html", images_dir / "daily_overview.png", 1920, overview_height, profile_dir)
            screenshot_count += 1
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
    result = {
        "instruments": len(payloads), "detail_html": len(payloads),
        "screenshots": screenshot_count, "output_dir": str(output_dir),
    }
    (output_dir / "visual_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--config", type=Path, default=here / "contracts.json")
    parser.add_argument("--echarts", type=Path, default=here / "assets" / "echarts.min.js")
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    chrome = find_chrome(args.chrome)
    try:
        result = generate_visuals(
            args.data_dir, args.output_dir, args.config, args.echarts,
            chrome, not args.skip_images, args.overwrite,
        )
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
