"""Service catalog, playground and owner-scoped execution data."""
import datetime as dt
import json
import re
from typing import Literal
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import update
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Agent, User, iso_utc
from ..service_models import CustomService, ServiceRun
from ..security import can_manage, can_use, is_root, require_module, scope_owned
from .. import custom_services
from ..guardrail_policies import ContentBlocked

router=APIRouter(prefix="/api/v1/services",tags=["服务"])


class InputField(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    type:Literal["string","number","boolean","object","array"]="string"
    required:StrictBool=False


class ServiceConfig(BaseModel):
    model_config=ConfigDict(extra="forbid")
    url:str=Field(default="",max_length=2048)
    method:Literal["GET","POST"]="POST"
    headers:dict[str,str]=Field(default_factory=dict,max_length=32)
    command:str=Field(default="",max_length=1024)
    args:list[str]=Field(default_factory=list,max_length=64)
    cwd:str=Field(default="",max_length=2048)
    env:dict[str,str]=Field(default_factory=dict,max_length=32)
    timeout_seconds:int=Field(default=30,ge=1,le=120)


class ServiceBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=1,max_length=128)
    description:str=Field(default="",max_length=4000)
    kind:Literal["http","program"]="http"
    config:ServiceConfig=Field(default_factory=ServiceConfig)
    input_fields:list[InputField]=Field(default_factory=list,max_length=64)
    agent_ids:list[int]=Field(default_factory=list,max_length=100)
    enabled:StrictBool=True
    is_public:StrictBool=False
    revision:int|None=Field(default=None,ge=1)


def output(row,user):
    config=json.loads(row.config)
    manage=can_manage(user,row)
    if not manage:
        config={"method":config.get("method"),"timeout_seconds":config.get("timeout_seconds")}
    else:
        for key in ("headers","env"):
            config[key]={name:"********" if value else "" for name,value in config.get(key,{}).items()}
    return {"id":row.id,"name":row.name,"description":row.description,"kind":row.kind,"config":config,
            "input_fields":json.loads(row.input_fields),"agent_ids":json.loads(row.agent_ids),
            "enabled":row.enabled,"is_public":row.is_public,"revision":row.revision,
            "can_manage":manage,"can_edit":manage and (row.kind!="program" or is_root(user)),
            "updated_at":iso_utc(row.updated_at)}


def validate_body(body,user,db,old=None):
    if body.kind=="program" or old is not None and old.kind=="program":
        if not is_root(user): raise HTTPException(403,"本地编程服务只能由 root 注册或修改；可使用已共享服务")
    if not body.name.strip(): raise HTTPException(400,"名称不能为空")
    config=body.config.model_dump()
    for key in ("headers","env"):
        previous=json.loads(old.config).get(key,{}) if old else {}
        for name,value in config[key].items():
            if value=="********":
                if name not in previous: raise HTTPException(400,"新增凭据须填写实际值")
                config[key][name]=previous[name]
            if len(value)>8192 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}",name):
                raise HTTPException(400,"环境变量或请求头格式无效")
    if {key.lower() for key in config["headers"]}&{"host","content-length","transfer-encoding","connection"}:
        raise HTTPException(400,"禁止覆盖传输请求头")
    if body.kind=="http":
        parsed=urlparse(config["url"])
        if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(400,"填写不包含凭据的 HTTP(S) 服务地址")
        config.update(command="",args=[],cwd="",env={})
    else:
        if not config["command"].strip() or config["command"].lower().endswith((".bat",".cmd",".ps1")):
            raise HTTPException(400,"填写可执行程序路径；脚本请通过显式解释器和参数运行")
        config.update(url="",headers={})
    for agent_id in set(body.agent_ids):
        agent=db.get(Agent,agent_id)
        if agent is None or not can_manage(user,agent): raise HTTPException(403,"只能绑定有管理权限的智能体")
    if len({field.name for field in body.input_fields})!=len(body.input_fields):
        raise HTTPException(400,"输入字段名称不能重复")
    return config


@router.get("")
def catalog(user:User=Depends(require_module("services")),db:Session=Depends(get_db)):
    return [output(row,user) for row in scope_owned(db.query(CustomService),CustomService,user).order_by(CustomService.id.desc()).all()]


@router.get("/targets")
def targets(user:User=Depends(require_module("services")),db:Session=Depends(get_db)):
    return {"agents":[{"id":a.id,"name":a.name} for a in db.query(Agent).all() if can_manage(user,a)],"can_program":is_root(user)}


@router.get("/runs")
def runs(user:User=Depends(require_module("services")),db:Session=Depends(get_db)):
    return [{"id":r.id,"service_name":r.service_name,"status":r.status,"duration_ms":r.duration_ms,"result":r.result,"created_at":iso_utc(r.created_at)}
            for r in db.query(ServiceRun).filter(ServiceRun.owner_id==user.id).order_by(ServiceRun.id.desc()).limit(100).all()]


@router.post("",status_code=201)
def create(body:ServiceBody,user:User=Depends(require_module("services")),db:Session=Depends(get_db)):
    config=validate_body(body,user,db)
    row=CustomService(name=body.name.strip(),description=body.description,kind=body.kind,config=json.dumps(config),
        input_fields=json.dumps([f.model_dump() for f in body.input_fields]),agent_ids=json.dumps(sorted(set(body.agent_ids))),
        enabled=body.enabled,is_public=body.is_public,created_by=user.id)
    db.add(row);db.commit();db.refresh(row)
    return output(row,user)


@router.put("/{service_id}")
def edit(service_id:int,body:ServiceBody,user:User=Depends(require_module("services")),db:Session=Depends(get_db)):
    row=db.get(CustomService,service_id)
    if row is None or not can_manage(user,row): raise HTTPException(404,"服务不存在或不可管理")
    config=validate_body(body,user,db,row)
    if body.revision!=row.revision: raise HTTPException(409,"服务已更新，请重新读取")
    result=db.execute(update(CustomService).where(CustomService.id==service_id,CustomService.revision==body.revision).values(
        name=body.name.strip(),description=body.description,kind=body.kind,config=json.dumps(config),
        input_fields=json.dumps([f.model_dump() for f in body.input_fields]),agent_ids=json.dumps(sorted(set(body.agent_ids))),
        enabled=body.enabled,is_public=body.is_public,revision=body.revision+1,updated_at=dt.datetime.now(dt.timezone.utc)))
    if result.rowcount!=1: db.rollback();raise HTTPException(409,"服务已更新，请重新读取")
    db.commit();db.expire_all()
    return output(db.get(CustomService,service_id),user)


@router.delete("/{service_id}",status_code=204)
def delete(service_id:int,user:User=Depends(require_module("services")),db:Session=Depends(get_db)):
    row=db.get(CustomService,service_id)
    if row is None or not can_manage(user,row): raise HTTPException(404,"服务不存在或不可管理")
    if row.kind=="program" and not is_root(user): raise HTTPException(403,"仅 root 可管理本地程序")
    db.delete(row);db.commit()


class RunBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    input:dict=Field(default_factory=dict)


@router.post("/{service_id}/run")
async def run(service_id:int,body:RunBody,user:User=Depends(require_module("services"))):
    try: return {"result":await custom_services.execute_service(service_id,body.input,user.id,playground=True)}
    except ContentBlocked as exc: raise HTTPException(403, str(exc)) from exc
    except ValueError as exc: raise HTTPException(400,str(exc)) from exc
