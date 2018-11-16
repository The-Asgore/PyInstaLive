import os
import shutil
import sys
import threading
import time
from xml.dom.minidom import parseString

from instagram_private_api import ClientConnectionError
from instagram_private_api import ClientError
from instagram_private_api import ClientThrottledError
from instagram_private_api_extensions import live
from instagram_private_api_extensions import replay

try:
    import logger
    import helpers
    import pil
    import dlfuncs
    from constants import Constants
    from comments import CommentsDownloader
except ImportError:
    from . import logger
    from . import helpers
    from . import pil
    from . import dlfuncs
    from .constants import Constants
    from .comments import CommentsDownloader


def get_stream_duration(compare_time, get_missing=False):
    try:
        had_wrong_time = False
        if get_missing:
            if int(time.time()) < int(compare_time):
                had_wrong_time = True
                corrected_compare_time = int(compare_time) - 5
                download_time = int(time.time()) - int(corrected_compare_time)
            else:
                download_time = int(time.time()) - int(compare_time)
            stream_time = int(time.time()) - int(pil.livestream_obj.get('published_time'))
            stream_started_mins, stream_started_secs = divmod(stream_time - download_time, 60)
        else:
            if int(time.time()) < int(compare_time):
                had_wrong_time = True
                corrected_compare_time = int(compare_time) - 5
                stream_started_mins, stream_started_secs = divmod((int(time.time()) - int(corrected_compare_time)), 60)
            else:
                stream_started_mins, stream_started_secs = divmod((int(time.time()) - int(compare_time)), 60)
        stream_duration_str = '%d minutes' % stream_started_mins
        if stream_started_secs:
            stream_duration_str += ' and %d seconds' % stream_started_secs
        if had_wrong_time:
            return "{:s} (corrected)".format(stream_duration_str)
        else:
            return stream_duration_str
    except Exception as e:
        return "Not available"


def get_user_id():
    is_user_id = False
    try:
        user_id = int(pil.dl_user)
        is_user_id = True
    except ValueError:
        try:
            user_res = pil.ig_api.username_info(pil.dl_user)
            user_id = user_res.get('user', {}).get('pk')
        except ClientConnectionError as cce:
            logger.error(
                "Could not get user info for '{:s}': {:d} {:s}".format(pil.dl_user, cce.code, str(cce)))
            if "getaddrinfo failed" in str(cce):
                logger.error('Could not resolve host, check your internet connection.')
            if "timed out" in str(cce):
                logger.error('The connection timed out, check your internet connection.')
            logger.separator()
        except ClientThrottledError as cte:
            logger.error(
                "Could not get user info for '{:s}': {:d} {:s}".format(pil.dl_user, cte.code, str(cte)))
            logger.separator()
        except ClientError as ce:
            logger.error(
                "Could not get user info for '{:s}': {:d} {:s}".format(pil.dl_user, ce.code, str(ce)))
            if "Not Found" in str(ce):
                logger.error('The specified user does not exist.')
            logger.separator()
        except Exception as e:
            logger.error("Could not get user info for '{:s}': {:s}".format(pil.dl_user, str(e)))
            logger.separator()
        except KeyboardInterrupt:
            logger.binfo("Aborted getting user info for '{:s}', exiting.".format(pil.dl_user))
            logger.separator()
    if is_user_id:
        logger.info("Getting info for '{:s}' successful. Assuming input is an user Id.".format(pil.dl_user))
    else:
        logger.info("Getting info for '{:s}' successful.".format(pil.dl_user))
    logger.separator()
    return user_id


def get_broadcasts_info():
    try:
        user_id = get_user_id()
        broadcasts = pil.ig_api.user_story_feed(user_id)
        pil.livestream_obj = broadcasts.get('broadcast')
        pil.replays_obj = broadcasts.get('post_live_item', {}).get('broadcasts', [])
    except Exception as e:
        logger.error('Could not finish checking: {:s}'.format(str(e)))
        if "timed out" in str(e):
            logger.error('The connection timed out, check your internet connection.')
        logger.separator()
    except KeyboardInterrupt:
        logger.binfo('Aborted checking for livestreams and replays, exiting.'.format(pil.dl_user))
        logger.separator()
    except ClientThrottledError as cte:
        logger.error('Could not check because you are making too many requests at this time.')
        logger.separator()


def merge_segments():
    try:
        if pil.run_at_finish:
            try:
                thread = threading.Thread(target=helpers.run_command, args=(pil.run_at_finish,))
                thread.daemon = True
                thread.start()
                logger.binfo("Launched finish command: {:s}".format(pil.run_at_finish))
            except Exception as e:
                logger.warn('Could not execute command: {:s}'.format(str(e)))

        live_mp4_file = '{}{}_{}_{}_live.mp4'.format(pil.dl_path, pil.datetime_compat, pil.dl_user,
                                                     pil.livestream_obj.get('id'))
        if pil.comment_thread_worker and pil.comment_thread_worker.is_alive():
            logger.info("Waiting for comment downloader to finish.")
            pil.comment_thread_worker.join()

        logger.info('Merging downloaded files into video.')
        try:
            pil.broadcast_downloader.stitch(live_mp4_file, cleartempfiles=False)
            logger.info('Successfully merged downloaded files into video.')
            helpers.remove_lock()
        except ValueError as e:
            logger.error('Could not merged downloaded files: {:s}'.format(str(e)))
            logger.error('Likely the download duration was too short and no temp files were saved.')
            helpers.remove_lock()
        except Exception as e:
            logger.error('Could not merge downloaded files: {:s}'.format(str(e)))
            helpers.remove_lock()
    except KeyboardInterrupt:
        logger.binfo('Aborted merging process, no video was created.')
        helpers.remove_lock()


def download_livestream():
    try:
        def print_status(sep=True):
            heartbeat_info = pil.ig_api.broadcast_heartbeat_and_viewercount(pil.livestream_obj.get('id'))
            viewers = pil.livestream_obj.get('viewer_count', 0)
            if sep:
                logger.separator()
            else:
                logger.info('Username    : {:s}'.format(pil.dl_user))
            logger.info('Viewers     : {:s} watching'.format(str(int(viewers))))
            logger.info('Airing time : {:s}'.format(get_stream_duration(pil.livestream_obj.get('published_time'))))
            logger.info('Status      : {:s}'.format(heartbeat_info.get('broadcast_status').title()))
            return heartbeat_info.get('broadcast_status') not in ['active', 'interrupted']

        mpd_url = (pil.livestream_obj.get('dash_manifest')
                   or pil.livestream_obj.get('dash_abr_playback_url')
                   or pil.livestream_obj.get('dash_playback_url'))

        pil.live_folder_path = '{}{}_{}_{}_live_downloads'.format(pil.dl_path, pil.datetime_compat,
                                                                  pil.dl_user, pil.livestream_obj.get('id'))
        pil.broadcast_downloader = live.Downloader(
            mpd=mpd_url,
            output_dir=pil.live_folder_path,
            user_agent=pil.ig_api.user_agent,
            max_connection_error_retry=3,
            duplicate_etag_retry=30,
            callback_check=print_status,
            mpd_download_timeout=3,
            download_timeout=3)
    except Exception as e:
        logger.error('Could not start downloading livestream: {:s}'.format(str(e)))
        logger.separator()
        helpers.remove_lock()
    try:
        broadcast_owner = pil.livestream_obj.get('broadcast_owner', {}).get('username')
        try:
            broadcast_guest = pil.livestream_obj.get('cobroadcasters', {})[0].get('username')
        except Exception:
            broadcast_guest = None
        if broadcast_owner != pil.dl_user:
            logger.binfo('This livestream is a dual-live, the owner is "{}".'.format(broadcast_owner))
            broadcast_guest = None
        if broadcast_guest:
            logger.binfo('This livestream is a dual-live, the current guest is "{}".'.format(broadcast_guest))
        logger.separator()
        print_status(False)
        # logger.info('MPD URL     : {:s}'.format(mpd_url))
        logger.separator()
        helpers.create_lock_folder()
        logger.info('Downloading livestream, press [CTRL+C] to abort.')

        if pil.run_at_start:
            try:
                thread = threading.Thread(target=helpers.run_command, args=(pil.run_at_start,))
                thread.daemon = True
                thread.start()
                logger.binfo("Launched start command: {:s}".format(pil.run_at_start))
            except Exception as e:
                logger.warn('Could not launch command: {:s}'.format(str(e)))

        if pil.dl_comments:
            try:
                comments_json_file = '{}{}_{}_{}_live_comments.json'.format(pil.dl_path, pil.datetime_compat,
                                                                            pil.dl_user, pil.livestream_obj.get('id'))
                pil.comment_thread_worker = threading.Thread(target=get_live_comments, args=(comments_json_file,))
                pil.comment_thread_worker.start()
            except Exception as e:
                logger.error('An error occurred while downloading comments: {:s}'.format(str(e)))
        pil.broadcast_downloader.run()
        logger.separator()
        logger.info('Download duration : {}'.format(get_stream_duration(int(pil.epochtime))))
        logger.info('Stream duration   : {}'.format(get_stream_duration(pil.livestream_obj.get('published_time'))))
        logger.info(
            'Missing (approx.) : {}'.format(get_stream_duration(int(pil.epochtime), get_missing=True)))
        logger.separator()
        merge_segments()
    except KeyboardInterrupt:
        logger.separator()
        logger.binfo('The download has been aborted.')
        logger.separator()
        logger.info('Download duration : {}'.format(get_stream_duration(int(pil.epochtime))))
        logger.info('Stream duration   : {}'.format(get_stream_duration(pil.livestream_obj.get('published_time'))))
        logger.info(
            'Missing (approx.) : {}'.format(get_stream_duration(int(pil.epochtime), get_missing=True)))
        logger.separator()
        if not pil.broadcast_downloader.is_aborted:
            pil.broadcast_downloader.stop()
            merge_segments()


def download_replays():
    try:
        try:
            logger.info('Amount of replays    : {:s}'.format(str(len(pil.replays_obj))))
            for replay_index, replay_obj in enumerate(pil.replays_obj):
                bc_dash_manifest = parseString(replay_obj.get('dash_manifest')).getElementsByTagName('Period')
                bc_duration_raw = bc_dash_manifest[0].getAttribute("duration")
                bc_minutes = (bc_duration_raw.split("H"))[1].split("M")[0]
                bc_seconds = ((bc_duration_raw.split("M"))[1].split("S")[0]).split('.')[0]
                logger.info(
                    'Replay {:s} duration    : {:s} minutes and {:s} seconds'.format(str(replay_index + 1), bc_minutes,
                                                                                     bc_seconds))
        except Exception as e:
            logger.warn("An error occurred while getting replay duration information: {:s}".format(str(e)))
        logger.separator()
        logger.info("Downloading replays, press [CTRL+C] to abort.")
        logger.separator()
        for replay_index, replay_obj in enumerate(pil.replays_obj):
            exists = False
            pil.livestream_obj = replay_obj
            if Constants.PYTHON_VER[0][0] == '2':
                directories = (os.walk(pil.dl_path).next()[1])
            else:
                directories = (os.walk(pil.dl_path).__next__()[1])

            for directory in directories:
                if (str(replay_obj.get('id')) in directory) and ("_live_" not in directory):
                    logger.binfo("Already downloaded a replay with ID '{:s}'.".format(str(replay_obj.get('id'))))
                    exists = True
            if not exists:
                current = replay_index + 1
                logger.info(
                    "Downloading replay {:s} of {:s} with ID '{:s}'.".format(str(current), str(len(pil.replays_obj)),
                                                                               str(replay_obj.get('id'))))
                pil.live_folder_path = '{}{}_{}_{}_replay_downloads'.format(pil.dl_path, pil.datetime_compat,
                                                                            pil.dl_user, pil.livestream_obj.get('id'))
                broadcast_downloader = replay.Downloader(
                    mpd=replay_obj.get('dash_manifest'),
                    output_dir=pil.live_folder_path,
                    user_agent=pil.ig_api.user_agent)
                if pil.use_locks:
                    helpers.create_lock_folder()
                replay_mp4_file = '{}{}_{}_{}_replay.mp4'.format(pil.dl_path, pil.datetime_compat,
                                                                 pil.dl_user, pil.livestream_obj.get('id'))

                comments_json_file = '{}{}_{}_{}_live_comments.json'.format(pil.dl_path, pil.datetime_compat,
                                                                            pil.dl_user, pil.livestream_obj.get('id'))

                pil.comment_thread_worker = threading.Thread(target=get_replay_comments, args=(comments_json_file,))

                broadcast_downloader.download(replay_mp4_file, cleartempfiles=False)

                if pil.dl_comments:
                    logger.info("Downloading replay comments.")
                    try:
                        get_replay_comments(comments_json_file)
                    except Exception as e:
                        logger.error('An error occurred while downloading comments: {:s}'.format(str(e)))

                logger.info("Finished downloading replay {:s} of {:s}.".format(str(current), str(len(pil.replays_obj))))
                try:
                    os.remove(os.path.join(pil.live_folder_path, 'folder.lock'))
                except Exception:
                    pass

                if current != len(pil.replays_obj):
                    logger.separator()

        logger.separator()
        logger.info("Finished downloading all available replays.")
        logger.separator()
        helpers.remove_lock()
        sys.exit(0)
    except Exception as e:
        logger.error('Could not save replay: {:s}'.format(str(e)))
        logger.separator()
        helpers.remove_lock()
        sys.exit(1)
    except KeyboardInterrupt:
        logger.separator()
        logger.binfo('The download has been aborted by the user, exiting.')
        logger.separator()
        try:
            shutil.rmtree(pil.live_folder_path)
        except Exception as e:
            logger.error("Could not remove temp folder: {:s}".format(str(e)))
            sys.exit(1)
        helpers.remove_lock()
        sys.exit(0)


def get_live_comments(comments_json_file):
    try:
        comments_downloader = CommentsDownloader(destination_file=comments_json_file)
        first_comment_created_at = 0

        try:
            while not pil.broadcast_downloader.is_aborted:
                if 'initial_buffered_duration' not in pil.livestream_obj and pil.broadcast_downloader.initial_buffered_duration:
                    pil.livestream_obj['initial_buffered_duration'] = pil.broadcast_downloader.initial_buffered_duration
                    comments_downloader.broadcast = pil.livestream_obj
                first_comment_created_at = comments_downloader.get_live(first_comment_created_at)
        except ClientError as e:
            if not 'media has been deleted' in e.error_response:
                logger.warn("Comment collection ClientError: %d %s" % (e.code, e.error_response))

        try:
            if comments_downloader.comments:
                comments_downloader.save()
                comments_log_file = comments_json_file.replace('.json', '.log')
                comment_errors, total_comments = CommentsDownloader.generate_log(
                    comments_downloader.comments, pil.epochtime, comments_log_file,
                    comments_delay=pil.broadcast_downloader.initial_buffered_duration)
                if len(comments_downloader.comments) == 1:
                    logger.info("Successfully saved 1 comment.")
                    os.remove(comments_json_file)
                    logger.separator()
                    return True
                else:
                    if comment_errors:
                        logger.warn(
                            "Successfully saved {:s} comments but {:s} comments are (partially) missing.".format(
                                str(total_comments), str(comment_errors)))
                    else:
                        logger.info("Successfully saved {:s} comments.".format(str(total_comments)))
                    os.remove(comments_json_file)
                    logger.separator()
                    return True
            else:
                logger.info("There are no available comments to save.")
                return False
                logger.separator()
        except Exception as e:
            logger.error('Could not save comments: {:s}'.format(str(e)))
            return False
    except KeyboardInterrupt as e:
        logger.binfo("Downloading livestream comments has been aborted.")
        return False


def get_replay_comments(comments_json_file):
    try:
        comments_downloader = CommentsDownloader(destination_file=comments_json_file)
        comments_downloader.get_replay()
        try:
            if comments_downloader.comments:
                comments_log_file = comments_json_file.replace('.json', '.log')
                comment_errors, total_comments = CommentsDownloader.generate_log(
                    comments_downloader.comments, pil.livestream_obj.get('published_time'), comments_log_file,
                    comments_delay=0)
                if total_comments == 1:
                    logger.info("Successfully saved 1 comment to logfile.")
                    os.remove(comments_json_file)
                    logger.separator()
                    return True
                else:
                    if comment_errors:
                        logger.warn(
                            "Successfully saved {:s} comments but {:s} comments are (partially) missing.".format(
                                str(total_comments), str(comment_errors)))
                    else:
                        logger.info("Successfully saved {:s} comments.".format(str(total_comments)))
                    os.remove(comments_json_file)
                    logger.separator()
                    return True
            else:
                logger.info("There are no available comments to save.")
                return False
        except Exception as e:
            logger.error('Could not save comments to logfile: {:s}'.format(str(e)))
            return False
    except KeyboardInterrupt as e:
        logger.binfo("Downloading replay comments has been aborted.")
        return False