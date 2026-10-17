from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.core.files.uploadhandler import FileUploadHandler


class UploadRequestCapHandler(FileUploadHandler):
    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        if content_length is not None and content_length > settings.MAX_UPLOAD_REQUEST_BYTES:
            raise RequestDataTooBig("The total upload request exceeds the server limit.")
        self.total_bytes = 0
        self.current_file_bytes = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.current_file_bytes = 0

    def receive_data_chunk(self, raw_data, start):
        size = len(raw_data)
        self.current_file_bytes += size
        self.total_bytes += size
        if self.current_file_bytes > settings.MAX_UPLOAD_FILE_BYTES:
            raise RequestDataTooBig("An uploaded file exceeds the server limit.")
        if self.total_bytes > settings.MAX_UPLOAD_REQUEST_BYTES:
            raise RequestDataTooBig("The total upload request exceeds the server limit.")
        return raw_data

    def file_complete(self, file_size):
        return None
