"""对象存储：桶管理 + S3 兼容密钥签发。

本模块有一条**不可妥协的铁律**：

    「空桶」和「查询失败」必须是两个不同的结果。

历史教训：早期用 CLI 时，空桶的 JSON 返回体里**根本没有 `data` 键**
（形如 ``{"prefixes": []}``），而失败时退出码非 0 但 stderr 可能为空。
当时用 `j.get("data", [])` 兜底，结果「查询失败」被静默成「0 个对象」，
于是「空桶」判定成立 → 差点删掉用户有数据的桶。

用官方 SDK 后失败会抛异常，这个坑天然被堵住了。但我们仍然**显式**检查，
把这个语义焊死：拿不到明确的空列表，就一律当「不确定」处理并拒绝删除。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import oci

from ..errors import OciApiError
from ..models import BucketView
from ..oci_gateway import AccountClient

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  命名空间
# --------------------------------------------------------------------------
def namespace(client: AccountClient) -> str:
    """对象存储命名空间。

    ⚠️ 命名空间是**按 tenancy 共享**的：同租户下多个 user 拿到的是同一个值，
       共享同一份 20 GiB 免费额度。号池里不能按「账号数 × 20 GiB」算总量。
    ⚠️ 取不到命名空间 = 该账号对象存储未开通（不是权限问题）。
    """
    try:
        return client.namespace()
    except Exception as exc:  # noqa: BLE001
        raise OciApiError(
            f"{client.account.label} 取不到对象存储命名空间，"
            f"通常表示该账号**未开通对象存储**：{exc}"
        ) from exc


# --------------------------------------------------------------------------
#  桶
# --------------------------------------------------------------------------
def list_buckets(client: AccountClient, *, with_stats: bool = False) -> list[BucketView]:
    """列出租户下所有桶。"""
    ns = namespace(client)
    raw = client.call_all(client.object_storage.list_buckets,
                          namespace_name=ns, compartment_id=client.compartment_id)

    views: list[BucketView] = []
    for b in raw:
        view = BucketView(
            name=b.name,
            namespace=ns,
            created=str(b.time_created) if getattr(b, "time_created", None) else None,
            storage_tier=getattr(b, "storage_tier", None),
        )
        views.append(view)

    if with_stats:
        for view in views:
            try:
                view.versioning = get_versioning(client, view.name)
                view.par_count = len(list_preauth_requests(client, view.name))
                objs = list_objects(client, view.name, raise_on_unknown=True)
                view.object_count = len(objs)
                view.size_bytes = sum(o["size"] for o in objs)
            except Exception as exc:  # noqa: BLE001
                log.info("统计桶 %s 失败：%s", view.name, exc)
    return views


def get_versioning(client: AccountClient, bucket: str) -> str:
    """桶的版本控制状态。

    ⚠️ 开了版本控制的桶，**删掉对象并不会释放空间** —— 旧版本仍然占着，
       而且要删版本还得走 ``delete_object_version``。删桶前必须看这个。
    """
    ns = namespace(client)
    b = client.call(client.object_storage.get_bucket, ns, bucket)
    return getattr(b, "versioning", "Disabled") or "Disabled"


def create_bucket(client: AccountClient, name: str) -> BucketView:
    """建桶。"""
    ns = namespace(client)
    details = oci.object_storage.models.CreateBucketDetails(
        name=name,
        compartment_id=client.compartment_id,
    )
    b = client.call(client.object_storage.create_bucket, ns, details)
    return BucketView(name=b.name, namespace=ns,
                      created=str(b.time_created) if getattr(b, "time_created", None) else None)


# --------------------------------------------------------------------------
#  对象
# --------------------------------------------------------------------------
#: 单页最多拉多少个对象（OCI 上限 1000）
OBJECT_PAGE_SIZE = 1000

#: 单个桶最多枚举多少个对象（防止把内存打爆）
MAX_OBJECTS_ENUMERATED = 20000


def list_objects(client: AccountClient, bucket: str, *,
                 limit: int | None = None,
                 raise_on_unknown: bool = True) -> list[dict]:
    """列出桶内对象。

    返回 ``[]`` **只代表「确认是空桶」**。

    ⚠️ 这里有两个实测出来的 SDK 细节，都跟 CLI 时代的老经验不一样：

    **细节 1：返回的不是 list，是 ``ListObjects`` 模型。**
        ``list_objects().data`` 的真实结构是::

            ListObjects(objects=[...], prefixes=[...], next_start_with=None)

        要取对象得读 ``.data.objects``。直接对 ``.data`` 做 for 会报
        ``TypeError: 'ListObjects' object is not iterable``。

    **细节 2：分页游标在响应体里，不在响应头里。**
        翻页靠 ``next_start_with``，**不是** ``opc-next-page`` 头。
        所以通用分页器 ``oci.pagination.list_call_get_all_results``
        对它无效（它只看头），必须手工循环。
        这也是为什么本函数不用 ``call_all``。

    **判空语义**：``objects == []`` 才算空桶；
    ``objects is None`` 表示形态异常（**不代表空桶**），默认抛错。
    这条铁律的来源是 CLI 时代的一次险情：空桶的 JSON 返回体里没有 data 键，
    当时用 ``or []`` 兜底，把「查询失败」静默成了「空桶」，差点删掉有数据的桶。
    """
    ns = namespace(client)
    items: list[dict] = []
    start_with: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "namespace_name": ns,
            "bucket_name": bucket,
            "limit": OBJECT_PAGE_SIZE,
        }
        if start_with:
            kwargs["start_with"] = start_with

        page = client.call(client.object_storage.list_objects, **kwargs)

        raw = getattr(page, "objects", None)
        if raw is None:
            message = (
                f"列出 {bucket} 的对象时，返回体的 objects 字段为 None，"
                f"无法确认桶是否为空（**不代表空桶**）。"
            )
            if raise_on_unknown:
                raise OciApiError(message + "出于安全考虑，已中止后续操作。")
            log.warning(message)
            return []

        for obj in raw:
            items.append({
                "name": obj.name,
                "size": getattr(obj, "size", 0) or 0,
                "time_created": (str(obj.time_created)
                                 if getattr(obj, "time_created", None) else None),
            })

        start_with = getattr(page, "next_start_with", None)
        if not start_with:
            break
        if limit is not None and len(items) >= limit:
            break
        if len(items) >= MAX_OBJECTS_ENUMERATED:
            log.warning("%s 的对象数超过 %d，已截断", bucket, MAX_OBJECTS_ENUMERATED)
            break

    return items


def list_object_versions(client: AccountClient, bucket: str) -> list[dict]:
    """列出版本控制产生的历史版本（只有开了版本控制的桶才有）。

    ⚠️ 返回的是 ``ObjectVersionCollection``，对象在 ``.items`` 里，
       而且这个接口的分页游标走的是响应头 ``opc-next-page``（与
       ``list_objects`` 相反），所以要读 headers。
    """
    ns = namespace(client)
    items: list[dict] = []
    page_cursor: str | None = None

    try:
        while True:
            kwargs: dict[str, Any] = {"namespace_name": ns, "bucket_name": bucket}
            if page_cursor:
                kwargs["page"] = page_cursor
            resp = client.call_response(
                client.object_storage.list_object_versions, **kwargs)

            collection = getattr(resp, "data", None)
            raw = getattr(collection, "items", None)
            if raw is None:
                log.info("桶 %s 的版本列表返回形态异常，跳过", bucket)
                return items

            for v in raw:
                items.append({
                    "name": v.name,
                    "version_id": getattr(v, "version_id", None),
                    "size": getattr(v, "size", 0) or 0,
                })

            page_cursor = (getattr(resp, "headers", None) or {}).get("opc-next-page")
            if not page_cursor:
                break
            if len(items) >= MAX_OBJECTS_ENUMERATED:
                break
    except Exception as exc:  # noqa: BLE001 —— 桶没开版本控制时这里会失败
        log.info("列对象版本失败（桶可能未开版本控制）：%s", exc)
    return items


# --------------------------------------------------------------------------
#  预认证请求（PAR）—— 删桶的隐藏拦路虎
# --------------------------------------------------------------------------
def list_preauth_requests(client: AccountClient, bucket: str) -> list:
    """列出桶上的预认证请求。

    ⚠️ 只要桶上还挂着 PAR **记录**，删桶就会被 409 拒绝，报
       ``PreauthenticatedRequestStillExists``。
       **过期的 PAR 同样会阻塞** —— OCI 只检查记录是否存在，不看是否过期。
       实测有 2022 年就过期的 PAR 到 2026 年还在挡路。
    """
    ns = namespace(client)
    try:
        return client.call_all(client.object_storage.list_preauthenticated_requests,
                               namespace_name=ns, bucket_name=bucket)
    except Exception as exc:  # noqa: BLE001
        log.info("列 PAR 失败 %s：%s", bucket, exc)
        return []


def delete_preauth_request(client: AccountClient, bucket: str, par_id: str) -> None:
    ns = namespace(client)
    client.call(client.object_storage.delete_preauthenticated_request,
                ns, bucket, par_id)


# --------------------------------------------------------------------------
#  删除桶（三段式：复核 → 清理 → 删除）
# --------------------------------------------------------------------------
@dataclass
class BucketDeletePlan:
    bucket: str
    namespace: str
    object_count: int
    total_bytes: int
    versioning: str
    version_count: int
    par_ids: list[str]
    force: bool = False
    blocked_reason: str | None = None

    @property
    def can_execute(self) -> bool:
        return self.blocked_reason is None

    @property
    def needs_par_cleanup(self) -> bool:
        return bool(self.par_ids)

    def render(self) -> str:
        lines = [
            "🗑 删除存储桶计划",
            f"  桶名：{self.bucket}",
            f"  命名空间：{self.namespace[:10]}…",
            f"  对象数：{self.object_count}（{self.total_bytes / 1024 ** 2:.2f} MiB）",
            f"  版本控制：{self.versioning}"
            + (f"（另有 {self.version_count} 个历史版本）" if self.version_count else ""),
            f"  预认证请求(PAR)：{len(self.par_ids)} 个"
            + ("（删桶前会自动清理）" if self.par_ids else ""),
        ]
        if self.blocked_reason:
            lines.append(f"  ⛔ 已阻止：{self.blocked_reason}")
        else:
            lines.append("  ⚠️ 此操作不可逆。")
        return "\n".join(lines)


def plan_delete_bucket(client: AccountClient, bucket: str, *, force: bool = False) -> BucketDeletePlan:
    """生成删桶计划。

    **永远先复核**：这里会重新拉一次对象清单，而不是信任调用方传来的快照。
    删除前必须重新查询——历史上正是「信任扫描快照」导致过险情。
    """
    ns = namespace(client)
    versioning = get_versioning(client, bucket)
    objects = list_objects(client, bucket, raise_on_unknown=True)  # 失败会抛
    versions = list_object_versions(client, bucket) if versioning == "Enabled" else []
    pars = list_preauth_requests(client, bucket)

    blocked = None
    if objects and not force:
        blocked = (
            f"桶里还有 {len(objects)} 个对象。为防误删数据，默认拒绝。\n"
            f"  先确认内容（/objects 命令），确实要清空再加 force 重试。"
        )
    if versioning == "Enabled" and versions and not force:
        blocked = (
            f"桶开了版本控制，除 {len(objects)} 个当前对象外还有 "
            f"{len(versions)} 个历史版本，删对象**不会释放空间**。\n"
            f"  需要一并清理版本后再删桶。"
        )

    return BucketDeletePlan(
        bucket=bucket,
        namespace=ns,
        object_count=len(objects),
        total_bytes=sum(o["size"] for o in objects),
        versioning=versioning,
        version_count=len(versions),
        par_ids=[p.id for p in pars],
        force=force,
        blocked_reason=blocked,
    )


def execute_delete_bucket(client: AccountClient, plan: BucketDeletePlan) -> str:
    """执行删桶。

    正确顺序：① 清空对象 → ② 删 PAR → ③ 删桶 → ④ 核验。
    **不能跳过 ②**，否则 409。
    """
    if not plan.can_execute:
        raise RuntimeError(plan.blocked_reason or "计划不可执行")

    ns = plan.namespace

    # ① 清空对象（再次实时查询，不信任计划里的快照）
    current = list_objects(client, plan.bucket, raise_on_unknown=True)
    if current and not plan.force:
        raise RuntimeError(
            f"复核时发现桶里仍有 {len(current)} 个对象，已中止删除。"
        )
    for obj in current:
        client.call(client.object_storage.delete_object, ns, plan.bucket, obj["name"])
    if current:
        log.info("已清空 %s 的 %d 个对象", plan.bucket, len(current))

    # ② 删 PAR（过期的也要删）
    pars = list_preauth_requests(client, plan.bucket)
    for par in pars:
        delete_preauth_request(client, plan.bucket, par.id)
    if pars:
        log.info("已清理 %s 的 %d 个 PAR", plan.bucket, len(pars))

    # ③ 删桶
    client.call(client.object_storage.delete_bucket, ns, plan.bucket)

    # ④ 核验
    remaining = [b.name for b in client.call_all(
        client.object_storage.list_buckets, namespace_name=ns,
        compartment_id=client.compartment_id)]
    if plan.bucket in remaining:
        raise OciApiError(f"删除已下发，但 {plan.bucket} 仍出现在桶列表里，请稍后复查")
    return f"✅ 桶 {plan.bucket} 已删除"


# --------------------------------------------------------------------------
#  S3 兼容密钥
# --------------------------------------------------------------------------
@dataclass
class S3Credential:
    """S3 兼容凭据。

    ⚠️ **Secret Key 只在创建那一刻返回一次**，之后任何 API 都读不回来。
       这是 OCI 的设计，不是权限问题。所以要么当场记下，要么重新建一个。
    """

    access_key: str      # = customer secret key 的 id（40 位十六进制）
    secret_key: str      # = key（44 字符 base64）
    display_name: str
    user_id: str

    def render(self, region: str, namespace: str) -> str:
        return "\n".join([
            "🔑 S3 兼容凭据（**Secret Key 只显示这一次**，请立即保存）",
            f"  显示名：{self.display_name}",
            f"  Access Key：{self.access_key}",
            f"  Secret Key：{self.secret_key}",
            "",
            "  端点：",
            f"    https://{namespace}.compat.objectstorage.{region}.oraclecloud.com",
            f"    https://{namespace}.compat.objectstorage.{region}.oci.customer-oci.com",
            "",
            "  ⚠️ 刚创建的密钥有 5~8 分钟传播延迟，期间会间歇性报",
            "     SignatureDoesNotMatch。这是正常现象，**不是密钥错**，等一会儿重试即可。",
        ])


def create_s3_credential(client: AccountClient, display_name: str) -> S3Credential:
    """签发一对 S3 兼容密钥（Customer Secret Key）。

    字段对应关系（已实测确认）：
        ``id``  → S3 Access Key（40 位十六进制）
        ``key`` → S3 Secret Key（44 字符 base64）
    """
    user_id = client.account.user
    if not user_id:
        raise OciApiError(
            "该账号没有配置 user OCID，无法签发 S3 密钥"
            "（instance_principal 模式下不支持）"
        )
    details = oci.identity.models.CreateCustomerSecretKeyDetails(display_name=display_name)
    # ⚠️ 参数顺序是 (details, user_id) —— 反了会报奇怪的参数错误
    resp = client.call(client.identity.create_customer_secret_key, details, user_id)
    return S3Credential(
        access_key=resp.id,
        secret_key=resp.key,
        display_name=display_name,
        user_id=user_id,
    )


def list_s3_credentials(client: AccountClient) -> list[dict]:
    """列出已有的 S3 密钥（**只有 id 和名称，没有 secret**）。"""
    user_id = client.account.user
    if not user_id:
        return []
    items = client.call_all(client.identity.list_customer_secret_keys, user_id)
    return [{
        "id": c.id,
        "display_name": c.display_name,
        "time_created": str(c.time_created) if getattr(c, "time_created", None) else None,
        "lifecycle_state": getattr(c, "lifecycle_state", None),
    } for c in items]


def s3_endpoints(region: str, ns: str) -> dict[str, str]:
    """S3 兼容端点（两套域名等效，都可用）。"""
    return {
        "compat": f"https://{ns}.compat.objectstorage.{region}.oraclecloud.com",
        "compat_alt": f"https://{ns}.compat.objectstorage.{region}.oci.customer-oci.com",
        "native": f"https://objectstorage.{region}.oraclecloud.com/n/{ns}/",
    }


#: boto3 必须用这套配置，否则会踩两个坑（见 docs/06-踩坑清单.md）
BOTO3_CONFIG_SNIPPET = '''\
from boto3 import client
from botocore.config import Config

s3 = client(
    "s3",
    endpoint_url=ENDPOINT,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name=REGION,
    config=Config(
        signature_version="s3v4",
        # 必须 path-style：OCI 的虚拟主机样式另有兼容限制
        s3={"addressing_style": "path", "payload_signing_enabled": False},
        # botocore >= 1.36 默认用 aws-chunked 流式上传，OCI 不支持，
        # 会报 "AWS chunked encoding not supported"
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
)
'''
