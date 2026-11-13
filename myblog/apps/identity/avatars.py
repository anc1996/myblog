"""
独立会员头像清洗流水线与安全交付网关 (/avatar-gateway/)。

安全契约：
1. 不使用 CustomImage，避免会员头像污染作者文章配图选择器；
2. 上传限制 <= 2 MiB、宽高 <= 2048px、总像素 <= 2048*2048，仅允许静态 JPEG/PNG/WebP；
3. 经 Pillow 13.0.0 校验后 EXIF 归正，并把像素复制进空白画布重编码为静态 WebP，彻底剥离隐写/元数据；
4. 先写存储再提交 SQL 新指针，旧键入队 AvatarPurgeTask 异步回收；
5. 网关统一返回 image/webp + X-Content-Type-Options: nosniff，停用或失效令牌一律拒绝交付。
"""

from io import BytesIO
import logging
import uuid
import warnings

from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, HttpResponse, HttpResponseNotAllowed, HttpResponseNotFound
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from identity.avatar_purge import enqueue_avatar_purge_task
from identity.avatar_storage import get_storage_and_namespace
from identity.models import MemberProfile, User

logger = logging.getLogger(__name__)

MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_DIM = 2048
MAX_AVATAR_PIXELS = 2048 * 2048
ALLOWED_PILLOW_FORMATS = {"JPEG", "PNG", "WEBP"}


def _read_bounded_bytes(uploaded_file, limit: int = MAX_AVATAR_BYTES) -> bytes:
    """按真实字节流分块读取，防止客户端伪造 Content-Length 或 file.size 绕过上限。"""
    if hasattr(uploaded_file, "seek"):
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
    chunks = []
    total = 0
    if hasattr(uploaded_file, "chunks"):
        iterator = uploaded_file.chunks()
    else:
        iterator = iter(lambda: uploaded_file.read(65536), b"")
    for chunk in iterator:
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ValidationError("头像文件大小不能超过 2 MB。")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise ValidationError("上传的头像文件为空。")
    return raw


def sanitize_and_encode_avatar(uploaded_file) -> bytes:
    """
    使用 Pillow 完整解码、防炸弹检查、EXIF 方向归正，并通过纯像素复制到新画布重编码为静态 WebP。
    """
    raw = _read_bounded_bytes(uploaded_file, limit=MAX_AVATAR_BYTES)

    head_lower = raw[:512].lstrip().lower()
    if head_lower.startswith(b"<svg") or b"<svg" in head_lower or head_lower.startswith(b"<?xml"):
        raise ValidationError("头像仅支持静态 JPG/PNG/WebP 位图，禁止上传 SVG 或 XML 文件。")

    prev_max_pixels = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_AVATAR_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                with Image.open(BytesIO(raw)) as probe:
                    fmt = (probe.format or "").upper()
                    if fmt not in ALLOWED_PILLOW_FORMATS:
                        raise ValidationError("头像格式仅支持静态 JPG、PNG 或 WebP。")
                    if getattr(probe, "is_animated", False) or getattr(probe, "n_frames", 1) > 1:
                        raise ValidationError("头像禁止上传动态图或多帧图像。")
                    w, h = probe.size
                    if w <= 0 or h <= 0 or w > MAX_AVATAR_DIM or h > MAX_AVATAR_DIM or (w * h) > MAX_AVATAR_PIXELS:
                        raise ValidationError("头像尺寸超出限制（宽高均须不超过 2048×2048）。")
                    probe.verify()

                with Image.open(BytesIO(raw)) as src:
                    src.load()
                    if getattr(src, "is_animated", False) or getattr(src, "n_frames", 1) > 1:
                        raise ValidationError("头像禁止上传动态图或多帧图像。")
                    src = ImageOps.exif_transpose(src)
                    w, h = src.size
                    if w <= 0 or h <= 0 or w > MAX_AVATAR_DIM or h > MAX_AVATAR_DIM or (w * h) > MAX_AVATAR_PIXELS:
                        raise ValidationError("头像尺寸超出限制（宽高均须不超过 2048×2048）。")

                    has_alpha = "A" in src.getbands() or "transparency" in src.info
                    target_mode = "RGBA" if has_alpha else "RGB"
                    converted = src.convert(target_mode)
                    # 仅复制纯像素进全新空白画布，彻底清除 EXIF/GPS/XMP/ICC 等附加信息
                    clean_canvas = Image.frombytes(target_mode, converted.size, converted.tobytes())
                    out = BytesIO()
                    clean_canvas.save(out, format="WEBP", quality=85, method=4)
                    return out.getvalue()
            except ValidationError:
                raise
            except UnidentifiedImageError:
                raise ValidationError("无法识别的图像文件，请上传有效的静态 JPG、PNG 或 WebP 图片。")
            except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise ValidationError("图像像素尺寸过大或存在解压缩风险，请压缩后再试。")
            except (
                OSError,
                ValueError,
            ) as exc:
                logger.warning("Avatar image sanitize failed (%s)", type(exc).__name__)
                raise ValidationError("头像图像无法安全解析，请更换正常静态图片重试。") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = prev_max_pixels


def upload_member_avatar(
    *,
    user: User,
    uploaded_file,
    storage_alias: str = "default",
    operation_id: uuid.UUID | None = None,
) -> MemberProfile:
    """
    为会员或后台用户上传并切换独立头像：
    先将重编码后的 WebP 写入私有存储，再在事务内更新数据库指针并将旧键入队异步清理。
    """
    webp_bytes = sanitize_and_encode_avatar(uploaded_file)
    op_id = operation_id or uuid.uuid4()
    storage, storage_ns = get_storage_and_namespace(storage_alias)
    new_token = uuid.uuid4()
    candidate_key = f"member-avatars/{uuid.uuid4().hex}/{uuid.uuid4().hex[:12]}.webp"
    saved_key = storage.save(candidate_key, ContentFile(webp_bytes))

    with transaction.atomic():
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        if not locked_user.is_active:
            raise PermissionDenied("账号已停用，无法更换头像。")
        profile, _ = MemberProfile.objects.select_for_update().get_or_create(user=locked_user)

        old_key = profile.avatar_key
        old_alias = profile.avatar_storage_alias or storage_alias
        old_ns = profile.avatar_storage_namespace or storage_ns

        wagtail_old_key = ""
        try:
            from wagtail.users.models import UserProfile

            wp = UserProfile.objects.select_for_update().filter(user_id=locked_user.id).first()
            if wp and wp.avatar and wp.avatar.name:
                wagtail_old_key = wp.avatar.name
                wp.avatar = ""
                wp.save(update_fields=["avatar"])
        except Exception:
            pass

        profile.avatar_token = new_token
        profile.avatar_version = (profile.avatar_version or 0) + 1
        profile.avatar_storage_alias = storage_alias
        profile.avatar_storage_namespace = storage_ns
        profile.avatar_key = saved_key
        profile.avatar_updated_at = timezone.now()
        profile.save()

        if old_key and (old_ns != storage_ns or old_key != saved_key):
            enqueue_avatar_purge_task(
                storage_alias=old_alias,
                storage_namespace=old_ns,
                storage_key=old_key,
                source="avatar_replace",
                operation_id=op_id,
                target_user_id_snapshot=locked_user.id,
            )
        if wagtail_old_key:
            _, default_ns = get_storage_and_namespace("default")
            enqueue_avatar_purge_task(
                storage_alias="default",
                storage_namespace=default_ns,
                storage_key=wagtail_old_key,
                source="legacy_migration",
                operation_id=op_id,
                target_user_id_snapshot=locked_user.id,
            )
        return profile


def migrate_legacy_wagtail_avatar_for_user(
    *,
    user: User,
    storage_alias: str = "default",
    operation_id: uuid.UUID | None = None,
) -> bool:
    """受控将旧 Wagtail UserProfile.avatar 清洗转存至 MemberProfile，并将旧键入队清理。"""
    from wagtail.users.models import UserProfile

    wp = UserProfile.objects.filter(user_id=user.id).first()
    if not wp or not wp.avatar or not wp.avatar.name:
        return False
    with wp.avatar.open("rb") as fh:
        upload_member_avatar(
            user=user,
            uploaded_file=fh,
            storage_alias=storage_alias,
            operation_id=operation_id,
        )
    return True


def serve_avatar_gateway(request, avatar_token: uuid.UUID):
    """
    专用会员头像交付网关：
    禁止前端传入对象键，只根据有效 avatar_token 与在用账号状态读取存储流并附安全响应头。
    """
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET", "HEAD"])

    profile = (
        MemberProfile.objects.select_related("user")
        .filter(avatar_token=avatar_token)
        .first()
    )
    if not profile or not profile.user.is_active or not profile.avatar_key:
        return HttpResponseNotFound("404 Not Found: Avatar unavailable")

    try:
        storage, current_ns = get_storage_and_namespace(profile.avatar_storage_alias or "default")
    except Exception:
        return HttpResponseNotFound("404 Not Found: Storage unavailable")

    if current_ns != profile.avatar_storage_namespace:
        return HttpResponseNotFound("404 Not Found: Avatar storage mismatch")

    try:
        file_obj = storage.open(profile.avatar_key, "rb")
    except Exception:
        return HttpResponseNotFound("404 Not Found: Avatar object missing")

    if request.method == "HEAD":
        try:
            size = getattr(file_obj, "size", None)
        except Exception:
            size = None
        finally:
            file_obj.close()
        resp = HttpResponse(status=200, content_type="image/webp")
        if size is not None:
            resp["Content-Length"] = str(size)
    else:
        resp = FileResponse(file_obj, content_type="image/webp")

    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-cache, must-revalidate"
    return resp
