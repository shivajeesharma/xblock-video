"""
Video XBlock mixins geared toward specific subsets of functionality.
"""
import logging

import requests
from pycaption import detect_format, WebVTTWriter
from webob import Response

from xblock.core import XBlock
from xblock.exceptions import NoSuchServiceError
from xblock.fields import Scope, Boolean, Float, String

from .constants import DEFAULT_LANG, TPMApiTranscriptFormatID, TPMApiLanguage, TranscriptSource
from .utils import create_reference_name, import_from, ugettext as _, underscore_to_mixedcase, Transcript

log = logging.getLogger(__name__)


@XBlock.wants('contentstore')
class ContentStoreMixin(XBlock):
    """
    Proxy to future `contentstore` service.

    If `contentstore` service is not provided by `runtime` it returns classes
    from `xmodule.contentstore`.

    At the time of writing `contentstore` service does not exist yet.
    """

    @property
    def contentstore(self):
        """
        Proxy to `xmodule.contentstore.contentstore` class.
        """
        contentstore_service = self.runtime.service(self, 'contentstore')
        if contentstore_service:
            return contentstore_service.contentstore

        return import_from('xmodule.contentstore.django', 'contentstore')

    @property
    def static_content(self):
        """
        Proxy to `xmodule.contentstore.StaticContent` class.
        """
        contentstore_service = self.runtime.service(self, 'contentstore')
        if contentstore_service:
            return contentstore_service.StaticContent

        return import_from('xmodule.contentstore.content', 'StaticContent')


class TranscriptsMixin(XBlock):
    """
    TranscriptsMixin class to encapsulate transcripts-related logic.
    """

    THREE_PLAY_MEDIA_API_DOMAIN = 'https://static.3playmedia.com/'

    threeplaymedia_streaming = Boolean(
        default=False,
        display_name=_('Direct 3PlayMedia'),
        scope=Scope.content,
        help=_("Direct <a href='http://www.3playmedia.com/'>3PlayMedia</a> transcripts usage enabled.")
    )

    threeplaymedia_apikey = String(
        default='',
        display_name=_('API Key'),
        help=_('You can generate a client token following official documentation of your video platform\'s API.'),
        scope=Scope.content,
        resettable_editor=False
    )

    threeplaymedia_file_id = String(
        default='',
        display_name=_('File Id'),
        help=_('3playmedia file id for download bind transcripts.'),
        scope=Scope.content,
        resettable_editor=False
    )

    @staticmethod
    def convert_caps_to_vtt(caps):
        """
        Utility method to convert any supported transcripts into WebVTT format.

        Supported input formats: DFXP/TTML - SAMI - SCC - SRT - WebVTT.

        Arguments:
            caps (unicode): Raw transcripts.
        Returns:
            unicode: Transcripts converted into WebVTT format.
        """
        reader = detect_format(caps)
        if reader:
            return WebVTTWriter().write(reader().read(caps))
        return u''

    def route_transcripts(self):
        """
        Re-route transcripts to appropriate handler.

        While direct 3PlayMedia transcripts enabled: to transcript fetcher
        and to `str_to_vtt` handler for non .vtt transcripts if opposite.

        Arguments:
            transcripts (unicode): Raw transcripts.
        """
        transcripts = self.get_enabled_transcripts()
        for tran in transcripts:
            if self.threeplaymedia_streaming:
                tran['url'] = self.runtime.handler_url(
                    self, 'fetch_from_three_play_media', query="{}={}".format(tran['lang_id'], tran['id'])
                )
            elif not tran['url'].endswith('.vtt'):
                tran['url'] = self.runtime.handler_url(
                    self, 'srt_to_vtt', query=tran['url']
                )
            yield tran

    def get_transcript_download_link(self):
        """
        Return link for downloading of a transcript of the current captions' language (if a transcript exists).
        """
        transcripts = self.get_enabled_transcripts()
        for transcript in transcripts:
            if transcript.get('lang') == self.captions_language:
                return transcript.get('url')
        return ''

    def create_transcript_file(self, ext='.vtt', trans_str='', reference_name=''):
        """
        Upload a transcript, fetched from a video platform's API, to video xblock.

        Arguments:
            ext (str): format of transcript file, default is vtt.
            trans_str (str): multiple string for convert to vtt file.
            reference_name (str): name of transcript file.
        Returns:
            File's file_name and external_url.
        """
        # Define location of default transcript as a future asset and prepare content to store in assets
        file_name = reference_name.replace(" ", "_") + ext
        course_key = self.course_key
        content_loc = self.static_content.compute_location(course_key, file_name)  # AssetLocator object
        content = self.static_content(
            content_loc,
            file_name,
            'application/json',
            trans_str.encode('UTF-8')
        )  # StaticContent object
        external_url = '/' + str(content_loc)

        # Commit the content
        self.contentstore().save(content)

        return file_name, external_url

    def convert_3playmedia_caps_to_vtt(self, caps, video_id, lang="en", lang_label="English"):
        """
        Utility method to convert any supported transcripts into WebVTT format.

        Arguments:
            caps (unicode)  : Raw transcripts.
            video_id (str)  : Video id from player.
            lang (str)      : Iso code for language.
            lang_label (str): Name of language.
        Returns:
            response (dict) : {"lang": lang, "url": url, "label": lang_label}
                lang (str)  : Iso code for language.
                url (str)   : External url for vtt file.
                label (str) : Name of language.
        """
        out, response = [], {}
        for item in caps.splitlines():
            if item == '':
                item = ' \n'
            elif '-->' in item:
                # This line is deltatime stamp 00:05:55.030 --> 00:05:57.200.
                # Length this line is 29 characters.
                item = item[:29]
            out.append(item)

        caps = u'\n'.join(out).replace('\n&nbsp;', '')
        sub = self.convert_caps_to_vtt(caps=caps)
        reference_name = "{lang_label}_captions_video_{video_id}".format(
            lang_label=lang_label, video_id=video_id
        ).encode('utf8')
        file_name, external_url = self.create_transcript_file(
            trans_str=sub, reference_name=reference_name
        )
        if file_name:
            response = {"lang": lang, "url": external_url, "label": lang_label}
        return response

    def fetch_available_3pm_transcripts(self):
        """
        Fetch all available transcripts from 3PlayMedia API for current file ID.

        :return: (generator of OrderedDicts) all transcript's data
        """
        results = self.get_available_3pm_transcripts(self.threeplaymedia_file_id, self.threeplaymedia_apikey)
        errors = isinstance(results, dict) and results.get('errors')
        log.debug("Fetched 3PM transcripts:\n{}".format(results))
        if errors:
            log.error("3PlayMedia transcripts fetching API request has failed!\n{}".format(
                results.get('errors')
            ))
            raise StopIteration
        for transcript_data in results:
            transcript = self.fetch_3pm_translation(transcript_data)
            if transcript is None:
                raise StopIteration
            transcript_ordered_dict = transcript._asdict()
            transcript_ordered_dict['content'] = ''  # we don't want to parse it to JSON
            yield transcript_ordered_dict

    def get_available_3pm_transcripts(self, file_id, apikey):
        """
        Make API request to fetch list of available transcripts for given file ID.

        :return: (list of dicts OR dict) all available transcripts attached to file with ID OR error dict
        """
        domain = self.THREE_PLAY_MEDIA_API_DOMAIN
        response = requests.get(
            '{domain}files/{file_id}/transcripts?apikey={api_key}'.format(
                domain=domain, file_id=file_id, api_key=apikey
            )
        )
        if response.ok:
            results = response.json()
        else:
            results = {"failed": True}
        return results

    def fetch_3pm_translation(self, transcript_data, format_id=TPMApiTranscriptFormatID.WEBVTT):
        """
        Fetch single transcript for given file ID in given format.

        :param transcript_data:
        :param format_id: defauts to VTT
        :return: (namedtuple instance) transcript data
        """
        tid = transcript_data.get('id', '')
        lang_id = transcript_data.get('language_id')
        external_api_url = '{domain}files/{file_id}/transcripts/{tid}?apikey={api_key}&format_id={format_id}'.format(
            domain=self.THREE_PLAY_MEDIA_API_DOMAIN,
            file_id=self.threeplaymedia_file_id,
            tid=tid,
            api_key=self.threeplaymedia_apikey,
            format_id=format_id
        )
        try:
            content = requests.get(external_api_url).text
        except Exception:  # pylint: disable=broad-except
            log.exception(_("Transcript fetching failure: language [{}]").format(TPMApiLanguage(lang_id)))
            return

        lang_code = TPMApiLanguage(lang_id)
        lang_label = lang_code.name
        video_id = self.get_player().media_id(self.href)
        source = TranscriptSource.THREE_PLAY_MEDIA
        return Transcript(
            id=tid,
            content=content,
            lang=lang_code.iso_639_1_code,
            lang_id=lang_id,
            label=lang_label,
            video_id=video_id,
            format=format_id,
            source=source,
            url=external_api_url,
        )

    @XBlock.handler
    def download_transcript(self, request, _suffix=''):
        """
        Download a transcript.

        Arguments:
            request (webob.Request): Request to handle.
            suffix (string): Slug used for routing.
        Returns:
            File with the correct name.
        """
        trans_path = self.get_path_for(request.query_string)
        filename = self.get_file_name_from_path(trans_path)
        transcript = requests.get(request.host_url + request.query_string).text
        response = Response(transcript)
        headerlist = [
            ('Content-Type', 'text/plain'),
            ('Content-Disposition', 'attachment; filename={}'.format(filename))
        ]
        response.headerlist = headerlist
        return response

    @XBlock.handler
    def srt_to_vtt(self, request, _suffix=''):
        """
        Fetch raw transcripts, convert them into WebVTT format and return back.

        Path to raw transcripts is passed in as `request.query_string`.

        Arguments:
            request (webob.Request): The request to handle
            suffix (string): The remainder of the url, after the handler url prefix, if available.
        Returns:
            webob.Response: WebVTT transcripts wrapped in Response object.
        """
        caps_path = request.query_string
        caps = requests.get(request.host_url + caps_path).text
        return Response(self.convert_caps_to_vtt(caps))

    @XBlock.handler
    def fetch_from_three_play_media(self, request, _suffix=''):
        """
        Proxy handler to hide real API url.

        Arguments:
            request (webob.Request): The request to handle
            suffix (string): not used
            query string: 'language_id=transcript_id'
        Returns:
            webob.Response: WebVTT transcripts wrapped in Response object.
        """
        lang_id, tid = request.query_string.split('=')
        transcript = self.fetch_3pm_translation(transcript_data={'id': tid, 'language_id': lang_id})
        if transcript is None:
            return Response()
        return Response(transcript.content)

    @XBlock.handler
    def validate_three_play_media_config(self, request, _suffix=''):
        """
        Handler to validate actuality of provided API credentials.

        Arguments:
            request (webob.Request):
            suffix (string): not used
        Returns:
            webob.Response: (json) {'isValid': true/false}
        """
        api_key = request.json.get('api_key')
        file_id = request.json.get('file_id')
        streaming_enabled = request.json.get('streaming_enabled')

        # the very first request during xblock creating:
        if api_key is None and file_id is None:
            return Response(json={'isValid': True, 'message': _("Initialization")})

        # the case when no options provided, and streaming is disabled:
        success_message = _('Success')
        if not api_key and not file_id and not streaming_enabled:
            return Response(json={'isValid': True, 'message': success_message})

        # options partially provided or both empty, but streaming is enabled:
        message = _('Check provided 3PlayMedia configuration')
        if not (api_key and file_id):
            return Response(json={'isValid': False, 'message': message})

        results = self.get_available_3pm_transcripts(file_id, api_key)

        is_valid = False if isinstance(results, dict) else True
        if is_valid:
            message = success_message

        return Response(json={'isValid': is_valid, 'message': message})


@XBlock.needs('modulestore')
class PlaybackStateMixin(XBlock):
    """
    PlaybackStateMixin encapsulates video-playback related data.

    These fields are not visible to end-user.
    """

    current_time = Float(
        default=0,
        scope=Scope.user_state,
        help='Seconds played back after the start'
    )

    playback_rate = Float(
        default=1,
        scope=Scope.preferences,
        help='Supported video playbacks speeds are: 0.5, 1, 1.5, 2'
    )

    volume = Float(
        default=1,
        scope=Scope.preferences,
        help='Video volume: from 0 to 1'
    )

    muted = Boolean(
        default=False,
        scope=Scope.preferences,
        help="Video is muted or not"
    )

    captions_language = String(
        default='',
        scope=Scope.preferences,
        help="ISO code for the current language for captions and transcripts"
    )

    transcripts = String(
        default='',
        scope=Scope.content,
        display_name=_('Upload transcript'),
        help=_(
            'Add transcripts in different languages. Click below to specify a language and upload an .srt transcript'
            ' file for that language.'
        )
    )

    transcripts_enabled = Boolean(
        default=False,
        scope=Scope.preferences,
        help="Transcripts are enabled or not"
    )

    captions_enabled = Boolean(
        default=False,
        scope=Scope.preferences,
        help="Captions are enabled or not"
    )

    player_state_fields = (
        'current_time', 'muted', 'playback_rate', 'volume', 'transcripts_enabled',
        'captions_enabled', 'captions_language', 'transcripts'
    )

    @property
    def course_default_language(self):
        """
        Utility method returns course's language.

        Falls back to 'en' if runtime doen't provide `modulestore` service.
        """
        try:
            course = self.runtime.service(self, 'modulestore').get_course(self.course_id)
            return course.language
        except NoSuchServiceError:
            return DEFAULT_LANG

    @property
    def player_state(self):
        """
        Return video player state as a dictionary.
        """
        transcripts = self.get_enabled_transcripts()
        transcripts_object = {
            trans['lang']: {'url': trans['url'], 'label': trans['label']}
            for trans in transcripts
        }
        state = {
            'captionsLanguage': self.captions_language or self.course_default_language,
            'transcriptsObject': transcripts_object,
            'transcripts': transcripts
        }
        for field_name in self.player_state_fields:
            mixedcase_field_name = underscore_to_mixedcase(field_name)
            state.setdefault(mixedcase_field_name, getattr(self, field_name))
        return state

    @player_state.setter
    def player_state(self, state):
        """
        Save video player state passed in as a dict into xblock's fields.

        Arguments:
            state (dict): Video player state key-value pairs.
        """
        for field_name in self.player_state_fields:
            setattr(self, field_name, state.get(field_name, getattr(self, field_name)))

    @XBlock.json_handler
    def save_player_state(self, request, _suffix=''):
        """
        Xblock handler to save playback player state. Called by JavaScript of `student_view`.

        Arguments:
            request (dict): Request data to handle.
            suffix (str): Slug used for routing.
        Returns:
            Data on success (dict).
        """
        player_state = {
            'transcripts': self.transcripts
        }

        for field_name in self.player_state_fields:
            if field_name not in player_state:
                player_state[field_name] = request[underscore_to_mixedcase(field_name)]

        self.player_state = player_state
        return {'success': True}


@XBlock.wants('settings')
class SettingsMixin(XBlock):
    """
    SettingsMixin provides access to XBlock settings service.

    Provides convenient access to XBlock's settings set in edx-platform config files.

    Sample default settings in /edx/app/edxapp/cms.env.json:
    "XBLOCK_SETTINGS": {
      "video_xblock": {
        "3playmedia_api_key": "987654321",
        "account_id": "1234567890"
      }
    }
    """

    block_settings_key = 'video_xblock'

    @property
    def settings(self):
        """
        Return xblock settings set in .json config.

        Returned value depends on the context:
        - `studio_view()` is being executed in CMS context and gets data from `lms.env.json`.
        - `student_view` is being executed in LMS context and gets data from `lms.env.json`.

        Returns:
            dict: Settings from config file. E.g.
                {
                    "threeplaymedia_apikey": "987654321",
                    "account_id": "1234567890"
                }
        """
        s_service = self.runtime.service(self, 'settings')
        if s_service:
            # At the moment SettingsService is not available in the context
            # of Studio Edit modal. See https://github.com/edx/edx-platform/pull/14648
            return s_service.get_settings_bucket(self)

        settings = import_from('django.conf', 'settings')
        return settings.XBLOCK_SETTINGS.get(self.block_settings_key, {})

    def populate_default_values(self, fields_dict):
        """
        Populate unset default values from settings file.
        """
        for key, value in self.settings.items():
            fields_dict.setdefault(key, value)
        return fields_dict


class LocationMixin(XBlock):
    """
    Provides utility methods to access XBlock's `location`.

    Some runtimes, e.g. workbench, don't provide location, hence stubs.
    """

    @property
    def block_id(self):
        """
        Facade property for `XBlock.location.block_id`.

        Returns stub value if `location` property is unavailabe. E.g. in workbench runtime.
        """
        if hasattr(self, 'location'):
            return self.location.block_id
        return 'block_id'

    @property
    def course_key(self):
        """
        Facade property for `XBlock.location.course_key`.

        Returns stub value if `location` property is unavailabe. E.g. in workbench runtime.
        """
        if hasattr(self, 'location'):
            return self.location.course_key
        return 'course_key'

    @property
    def usage_id(self):
        """
        Facade property for `XBlock.location.course_key`.

        Returns stub value if `location` property is unavailabe. E.g. in workbench runtime.
        """
        if hasattr(self, 'location'):
            return self.location.to_deprecated_string()
        return 'usage_id'
