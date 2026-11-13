"""
全站受控图片与文档管理表单 (P0-2)。

拦截对已被页面版本引用的媒体资源执行“直接替换底层文件”的破坏性操作：
只要 MediaReferenceRecord 中仍有引用，只允许修改标题/标签等非破坏性元数据，禁止替换 file。
"""

from django import forms
from wagtail.documents.forms import BaseDocumentForm
from wagtail.images.forms import BaseImageForm

from media_library.models import MediaReferenceRecord


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
