"""
全站受控图片、文档、音频与视频管理表单 (P0-2)。

拦截对已被页面版本引用的媒体资源执行“直接替换底层文件”的破坏性操作：
只要 MediaReferenceRecord 中仍有引用，只允许修改标题/标签等非破坏性元数据，禁止替换 file。
同时为音频、视频弹窗上传提供扩展名、MIME 与文件体积校验。
"""

from pathlib import Path
from django.conf import settings
from django import forms
from wagtail.documents.forms import BaseDocumentForm
from wagtail.images.forms import BaseImageForm

from media_library.models import Audio, MediaReferenceRecord, Video


class ProtectedImageForm(BaseImageForm):
    """防止替换正在被页面版本引用的图片原文件。"""

    def clean_file(self):
        file_val = self.cleaned_data.get("file")
        if self.instance and self.instance.pk and "file" in self.changed_data:
            if MediaReferenceRecord.objects.filter(asset_id=self.instance.public_id).exists():
                raise forms.ValidationError(
                    "该图片正被页面版本引用，禁止直接覆盖替换原文件内容；请上传新图片后再替换页面引用。"
                )
        return file_val


class ProtectedDocumentForm(BaseDocumentForm):
    """防止替换正在被页面版本引用的文档原文件。"""

    def clean_file(self):
        file_val = self.cleaned_data.get("file")
        if self.instance and self.instance.pk and "file" in self.changed_data:
            if MediaReferenceRecord.objects.filter(asset_id=self.instance.public_id).exists():
                raise forms.ValidationError(
                    "该文档正被页面版本引用，禁止直接覆盖替换原文件内容；请上传新文档后再替换页面引用。"
                )
        return file_val


def _validate_av_upload(file_obj, *, allowed_exts: tuple[str, ...], max_size: int, kind_label: str, expected_mime_prefix: str):
    if not file_obj:
        return file_obj
    name = getattr(file_obj, "name", "") or ""
    ext = Path(name).suffix.lower()
    if ext not in allowed_exts:
        raise forms.ValidationError(
            f"不支持的{kind_label}格式（仅允许: {', '.join(allowed_exts)}）"
        )
    size = getattr(file_obj, "size", 0) or 0
    if size > max_size:
        raise forms.ValidationError(
            f"{kind_label}大小超过允许上限（最大 {max_size // (1024 * 1024)} MB）"
        )
    ctype = (getattr(file_obj, "content_type", "") or "").lower()
    if ctype and not (ctype.startswith(expected_mime_prefix) or ctype == "application/octet-stream"):
        raise forms.ValidationError(
            f"上传文件的 MIME 类型 ({ctype}) 与{kind_label}要求不符"
        )
    return file_obj


class AudioUploadForm(forms.ModelForm):
    """页面选择器弹窗与新建页共用的音频上传表单"""

    class Meta:
        model = Audio
        fields = ("title", "file")
        help_texts = {
            "title": "请输入音频标题（用于播放器显示与无障碍描述）",
            "file": "支持 .mp3 / .wav / .flac / .ogg / .m4a / .aac，最大 50MB",
        }

    def clean_file(self):
        file_val = self.cleaned_data.get("file")
        if self.instance and self.instance.pk and "file" in self.changed_data:
            if MediaReferenceRecord.objects.filter(asset_id=self.instance.public_id).exists():
                raise forms.ValidationError(
                    "该音频正被页面版本引用，禁止直接覆盖替换原文件；请上传新音频后再替换引用。"
                )
        allowed = tuple(
            getattr(settings, "MEDIA_AUDIO_ALLOWED_EXTENSIONS", (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"))
        )
        max_size = int(getattr(settings, "MEDIA_AUDIO_MAX_UPLOAD_SIZE", 50 * 1024 * 1024))
        return _validate_av_upload(
            file_val,
            allowed_exts=allowed,
            max_size=max_size,
            kind_label="音频",
            expected_mime_prefix="audio/",
        )


class AudioAdminEditorForm(AudioUploadForm):
    """主编/超管全量音频管理表单"""

    class Meta(AudioUploadForm.Meta):
        fields = ("title", "file", "visibility", "duration")


class VideoUploadForm(forms.ModelForm):
    """页面选择器弹窗与受限作者共用的视频上传表单"""

    class Meta:
        model = Video
        fields = ("title", "file")
        help_texts = {
            "title": "请输入视频标题（用于播放器显示与无障碍描述）",
            "file": "支持 .mp4 / .webm / .mov / .m4v，最大 300MB",
        }

    def clean_file(self):
        file_val = self.cleaned_data.get("file")
        if self.instance and self.instance.pk and "file" in self.changed_data:
            if MediaReferenceRecord.objects.filter(asset_id=self.instance.public_id).exists():
                raise forms.ValidationError(
                    "该视频正被页面版本引用，禁止直接覆盖替换原文件；请上传新视频后再替换引用。"
                )
        allowed = tuple(
            getattr(settings, "MEDIA_VIDEO_ALLOWED_EXTENSIONS", (".mp4", ".webm", ".mov", ".m4v"))
        )
        max_size = int(getattr(settings, "MEDIA_VIDEO_MAX_UPLOAD_SIZE", 300 * 1024 * 1024))
        return _validate_av_upload(
            file_val,
            allowed_exts=allowed,
            max_size=max_size,
            kind_label="视频",
            expected_mime_prefix="video/",
        )


class VideoAdminEditorForm(VideoUploadForm):
    """主编/超管全量视频管理表单（含封面图、可见性与时长）"""

    class Meta(VideoUploadForm.Meta):
        fields = ("title", "file", "poster", "visibility", "duration")
