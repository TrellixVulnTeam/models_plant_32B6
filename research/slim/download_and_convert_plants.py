# -*- coding: UTF-8 -*-
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
r"""Downloads and converts Plant data to TFRecords of TF-Example protos.

This module downloads the Plant data, uncompresses it, reads the files
that make up the Plant data and creates two TFRecord datasets: one for train
and one for test. Each TFRecord dataset is comprised of a set of TF-Example
protocol buffers, each of which contain a single image and label.

The script should take about a minute to run.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json

import csv

import math
import os
import random
import sys
import io
import re
from collections import defaultdict

import tensorflow as tf

from datasets import dataset_utils
from datasets import plants

SPLIT_NAME_TRAIN = 'train'
SPLIT_NAME_VALIDATION = 'validation'
CLASS_NAME_OTHERS = u'其他'  # 無法分類的類別要叫什麼名字，None的話就不特別分出這一類

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string(
    'dataset_dir',
    None,
    'The directory where the output TFRecords and temporary files are saved.')

# The URL where the Plant data can be downloaded.
_DATA_URL = 'http://download.tensorflow.org/example_images/flower_photos.tgz'

# The number of images in the validation set.
# _NUM_VALIDATION = 350
_RADIO_VALIDATION = 1.0 / 6

# Seed for repeatability.
_RANDOM_SEED = 0

# The number of shards per dataset split.
_NUM_SHARDS = 5


class ImageReader(object):
    """Helper class that provides TensorFlow image coding utilities."""

    def __init__(self):
        # Initializes function that decodes RGB JPEG data.
        self._decode_jpeg_data = tf.placeholder(dtype=tf.string)
        self._decode_jpeg = tf.image.decode_jpeg(self._decode_jpeg_data,
                                                 channels=3)

    def read_image_dims(self, sess, image_data):
        image = self.decode_jpeg(sess, image_data)
        return image.shape[0], image.shape[1]

    def decode_jpeg(self, sess, image_data):
        image = sess.run(self._decode_jpeg,
                         feed_dict={self._decode_jpeg_data: image_data})
        assert len(image.shape) == 3
        assert image.shape[2] == 3
        return image


def _get_file_info(fpath):
    import piexif
    import piexif.helper
    import json

    def stringify_dict(d):
        newd = {}
        for k, v in d.iteritems():
            if isinstance(v, unicode):
                v = str(v)
            newd[str(k)] = v
        return newd

    fname = os.path.basename(fpath)
    exif_dict = piexif.load(fpath)
    user_comment = piexif.helper.UserComment.load(
        exif_dict["Exif"][piexif.ExifIFD.UserComment])
    user_comment = json.loads(user_comment)
    user_comment = stringify_dict(user_comment)

    res = {
        "name": fname,
        # "label": label_map[user_comment["plantName"]],
        "label": user_comment["plantName"],
        "len_cm": user_comment["lengthInCentiMeter"] / 10,
        "len_pixel": user_comment["lengthInPixel"] / 1000,
    }
    return res


def _get_jpgs(dir_path):
    return [
        os.path.join(dir_path, filename)
        for filename in os.listdir(dir_path)
        if filename.lower().endswith('.jpg')
    ]


def _utf_8_encoder(unicode_csv_data):
    for line in unicode_csv_data:
        yield line.encode('utf-8')


def _get_filenames_and_classes(dataset_dir):
    """Returns a list of filenames and inferred class names.

    Args:
      dataset_dir: A directory containing a set of subdirectories representing
        class names. Each subdirectory should contain PNG or JPG encoded images.

    Returns:
      A list of image file paths, relative to `dataset_dir` and the list of
      subdirectories, representing class names.
    """
    # flower_root = os.path.join(dataset_dir, 'flower_photos')
    csv_path = os.path.join(dataset_dir, 'result.csv')

    directories = []
    class_names = []
    filename_class_tuples = []
    with io.open(csv_path, encoding='utf8') as csvfile:
        reader = csv.reader(_utf_8_encoder(csvfile), delimiter=',')
        for class_folder, class_name in reader:
            class_name = class_name.decode('utf8')
            class_folder_path = os.path.join(dataset_dir, class_folder)
            jpg_files = _get_jpgs(class_folder_path)
            for file_path in jpg_files:
                class_names.append(class_name)
                filename_class_tuples.append((file_path, class_name))

    class_names = normalize_class_names(class_names)
    return filename_class_tuples, class_names


def _get_dataset_filename(dataset_dir, split_name, shard_id):
    output_filename = 'plants_%s_%05d-of-%05d.tfrecord' % (
        split_name, shard_id, _NUM_SHARDS)
    return os.path.join(dataset_dir, output_filename)


def _convert_dataset(split_name, filenames, class_names_to_ids, dataset_dir):
    """Converts the given filenames to a TFRecord dataset.

    Args:
      split_name: The name of the dataset, either 'train' or 'validation'.
      filenames: A list of absolute paths to png or jpg images.
      class_names_to_ids: A dictionary from class names (strings) to ids
        (integers).
      dataset_dir: The directory where the converted datasets are stored.
    """
    assert split_name in [SPLIT_NAME_TRAIN, SPLIT_NAME_VALIDATION]

    num_per_shard = int(math.ceil(len(filenames) / float(_NUM_SHARDS)))

    with tf.Graph().as_default():
        image_reader = ImageReader()

        with tf.Session('') as sess:

            for shard_id in range(_NUM_SHARDS):
                output_filename = _get_dataset_filename(
                    dataset_dir, split_name, shard_id)

                start_ndx = shard_id * num_per_shard
                end_ndx = min((shard_id + 1) * num_per_shard,
                              len(filenames))

                with tf.python_io.TFRecordWriter(
                        output_filename) as tfrecord_writer:
                    for i in range(start_ndx, end_ndx):
                        sys.stdout.write(
                            '\r>> Converting image %d/%d shard %d\n' % (
                                i + 1, len(filenames), shard_id))
                        sys.stdout.flush()

                        # Read the filename:
                        filename, class_name = filenames[i]
                        image_data = tf.gfile.FastGFile(filename,
                                                        'rb').read()
                        height, width = image_reader.read_image_dims(sess,
                                                                     image_data)

                        # class_name = os.path.basename(
                        #     os.path.dirname(filenames[i]))
                        class_id = class_names_to_ids[class_name]

                        example = dataset_utils.image_to_tfexample(
                            image_data, b'jpg', height, width, class_id)
                        tfrecord_writer.write(example.SerializeToString())

    sys.stdout.write('\n')
    sys.stdout.flush()


def _clean_up_temporary_files(dataset_dir):
    """Removes temporary files used to create the dataset.

    Args:
      dataset_dir: The directory where the temporary files are stored.
    """
    filename = _DATA_URL.split('/')[-1]
    filepath = os.path.join(dataset_dir, filename)
    tf.gfile.Remove(filepath)

    tmp_dir = os.path.join(dataset_dir, 'flower_photos')
    tf.gfile.DeleteRecursively(tmp_dir)


def _dataset_exists(dataset_dir):
    for split_name in [SPLIT_NAME_TRAIN, SPLIT_NAME_VALIDATION]:
        for shard_id in range(_NUM_SHARDS):
            output_filename = _get_dataset_filename(
                dataset_dir, split_name, shard_id)
            if not tf.gfile.Exists(output_filename):
                return False
    return True


def _write_dataset_info_file(dataset_info, dataset_dir,
                             filename=plants.DATASET_INFO_FILENAME):
    labels_filename = os.path.join(dataset_dir, filename)
    _save_as_json(labels_filename, dataset_info)


def _save_as_json(filename, data):
    with tf.gfile.Open(filename, 'w') as f:
        json.dump(data, f)


def save_filenames_by_split(dataset_dir, training_filename_pairs,
                            validation_filename_pairs):
    def get_filename(pair):
        filename = pair[0]
        if filename.startswith(dataset_dir):
            filename = filename[len(dataset_dir):]
        return filename

    def get_filenames(pairs):
        return [get_filename(p) for p in pairs]

    dataset_dir = re.sub('/$', '', dataset_dir) + '/'
    labels_filename = os.path.join(dataset_dir, 'filenames_by_split.json')
    _save_as_json(labels_filename, {
        SPLIT_NAME_TRAIN: get_filenames(training_filename_pairs),
        SPLIT_NAME_VALIDATION: get_filenames(validation_filename_pairs),
    })


def run(dataset_dir):
    """Runs the download and conversion operation.

    Args:
      dataset_dir: The dataset directory where the dataset is stored.
    """
    if not tf.gfile.Exists(dataset_dir):
        tf.gfile.MakeDirs(dataset_dir)

    # if _dataset_exists(dataset_dir):
    #     print('Dataset files already exist. Exiting without re-creating them.')
    #     return

    # dataset_utils.download_and_uncompress_tarball(_DATA_URL, dataset_dir)
    photo_filenames, class_names = _get_filenames_and_classes(dataset_dir)
    class_names_to_ids = dict(zip(class_names, range(len(class_names))))

    # Divide into train and test:
    training_filename_pairs, validation_filename_pairs = split_dataset_by_directory(
        photo_filenames)
    save_filenames_by_split(dataset_dir, training_filename_pairs,
                            validation_filename_pairs)

    v_set = set([a[1] for a in validation_filename_pairs])
    print(len(v_set))
    # return

    # Write the labels file:
    labels_to_class_names = dict(zip(range(len(class_names)), class_names))
    dataset_utils.write_label_file(labels_to_class_names, dataset_dir)
    _write_dataset_info_file({
        SPLIT_NAME_TRAIN: len(training_filename_pairs),
        SPLIT_NAME_VALIDATION: len(validation_filename_pairs),
    }, dataset_dir)

    # Convert the training and validation sets.
    _convert_dataset(SPLIT_NAME_TRAIN, training_filename_pairs,
                     class_names_to_ids,
                     dataset_dir)
    _convert_dataset(SPLIT_NAME_VALIDATION, validation_filename_pairs,
                     class_names_to_ids,
                     dataset_dir)

    # _clean_up_temporary_files(dataset_dir)
    print('\nFinished converting the Plant dataset!')


def normalize_class_names(class_names):
    # 「其他」永遠定義為id = 0
    if CLASS_NAME_OTHERS:
        class_names = [c for c in class_names if c != CLASS_NAME_OTHERS]
    class_names = sorted(list(set(class_names)))
    if CLASS_NAME_OTHERS:
        class_names = [CLASS_NAME_OTHERS] + class_names
    return class_names


def split_dataset(photo_filenames):
    random.seed(_RANDOM_SEED)
    random.shuffle(photo_filenames)
    # 就算樣本數太少也至少要有1個validation樣本
    num_validation = int(_RADIO_VALIDATION * len(photo_filenames)) or 1
    training_filenames = photo_filenames[num_validation:]
    validation_filenames = photo_filenames[:num_validation]
    return training_filenames, validation_filenames


def _groupby_unsorted(items, key=lambda x: x):
    d = defaultdict(list)
    for item in items:
        d[key(item)].append(item)
    return d.items()


def split_dataset_by_directory(photo_filenames):
    # rulu: 不是很有效率，但為了邏輯分離，所以在列出檔名後才又依其所在資料夾shuffle

    def _get_class(tuple):
        return tuple[1]

    training_filenames = []
    validation_filenames = []

    for class_name, tuples in _groupby_unsorted(photo_filenames,
                                                key=_get_class):
        dir_files_map = {
            dir_: files for dir_, files in
            _groupby_unsorted(tuples, lambda pair: os.path.dirname(pair[0]))
        }
        directories = dir_files_map.keys()
        _training_dirs, _validation_dirs = split_dataset(list(directories))

        for dir_ in _training_dirs:
            training_filenames.extend(dir_files_map[dir_])

        for dir_ in _validation_dirs:
            validation_filenames.extend(dir_files_map[dir_])

    return training_filenames, validation_filenames


def main(_):
    if not FLAGS.dataset_dir:
        raise ValueError(
            'You must supply the dataset directory with --dataset_dir')

    run(FLAGS.dataset_dir)


if __name__ == '__main__':
    tf.app.run()
