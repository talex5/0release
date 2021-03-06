# Copyright (C) 2009, Thomas Leonard
# See the README file for details, or visit http://0install.net.

import os, subprocess, shutil, sys, re
from xml.dom import minidom
from zeroinstall import SafeException
from zeroinstall.injector import model
from zeroinstall.support import ro_rmtree
from logging import info, warn

sys.path.insert(0, os.environ['RELEASE_0REPO'])
from repo import registry, merge

import support, compile
from scm import get_scm

XMLNS_RELEASE = 'http://zero-install.sourceforge.net/2007/namespaces/0release'

valid_phases = ['commit-release', 'generate-archive']

TMP_BRANCH_NAME = '0release-tmp'

test_command = os.environ['0TEST']

def run_unit_tests(local_feed):
	print "Running self-tests..."
	exitstatus = subprocess.call([test_command, '--', local_feed])
	if exitstatus == 2:
		print "SKIPPED unit tests for %s (no 'test' command)" % local_feed
		return
	if exitstatus:
		raise SafeException("Self-test failed with exit status %d" % exitstatus)

def upload_archives(options, status, uploads):
	# For each binary or source archive in uploads, ensure it is available
	# from options.archive_dir_public_url

	# We try to do all the uploads together first, and then verify them all
	# afterwards. This is because we may have to wait for them to be moved
	# from an incoming queue before we can test them.

	def url(archive):
		return support.get_archive_url(options, status.release_version, archive)

	# Check that url exists and has the given size
	def is_uploaded(url, size):
		if url.startswith('http://TESTING/releases'):
			return True

		print "Testing URL %s..." % url
		try:
			actual_size = int(support.get_size(url))
		except Exception, ex:
			print "Can't get size of '%s': %s" % (url, ex)
			return False
		else:
			if actual_size == size:
				return True
			print "WARNING: %s exists, but size is %d, not %d!" % (url, actual_size, size)
			return False

	# status.verified_uploads is an array of status flags:
	description = {
		'N': 'Upload required',
		'A': 'Upload has been attempted, but we need to check whether it worked',
		'V': 'Upload has been checked (exists and has correct size)',
	}

	if status.verified_uploads is None:
		# First time around; no point checking for existing uploads
		status.verified_uploads = 'N' * len(uploads)
		status.save()

	while True:
		print "\nUpload status:"
		for i, stat in enumerate(status.verified_uploads):
			print "- %s : %s" % (uploads[i], description[stat])
		print

		# Break if finished
		if status.verified_uploads == 'V' * len(uploads):
			break

		# Find all New archives
		to_upload = []
		for i, stat in enumerate(status.verified_uploads):
			assert stat in 'NAV'
			if stat == 'N':
				to_upload.append(uploads[i])
				print "Upload %s/%s as %s" % (status.release_version, uploads[i], url(uploads[i]))

		cmd = options.archive_upload_command.strip()

		if to_upload:
			# Mark all New items as Attempted
			status.verified_uploads = status.verified_uploads.replace('N', 'A')
			status.save()

			# Upload them...
			if cmd:
				support.show_and_run(cmd, to_upload)
			else:
				if len(to_upload) == 1:
					print "No upload command is set => please upload the archive manually now"
					raw_input('Press Return once the archive is uploaded.')
				else:
					print "No upload command is set => please upload the archives manually now"
					raw_input('Press Return once the %d archives are uploaded.' % len(to_upload))

		# Verify all Attempted uploads
		new_stat = ''
		for i, stat in enumerate(status.verified_uploads):
			assert stat in 'AV', status.verified_uploads
			if stat == 'A' :
				if not is_uploaded(url(uploads[i]), os.path.getsize(uploads[i])):
					print "** Archive '%s' still not uploaded! Try again..." % uploads[i]
					stat = 'N'
				else:
					stat = 'V'
			new_stat += stat

		status.verified_uploads = new_stat
		status.save()

		if 'N' in new_stat and cmd:
			raw_input('Press Return to try again.')

legacy_warning = """*** Note: the upload functions of 0release
*** (--archive-dir-public-url, --master-feed-file, --archive-upload-command
*** and --master-feed-upload-command) are being replaced by 0repo. They may
*** go away in future. If 0repo is not suitable for your needs, please
*** contact the mailing list to let us know.
***
***   http://www.0install.net/0repo.html
***   http://www.0install.net/support.html#lists
"""

def do_version_substitutions(impl_dir, version_substitutions, new_version):
	for (rel_path, subst) in version_substitutions:
		assert not os.path.isabs(rel_path), rel_path
		path = os.path.join(impl_dir, rel_path)
		with open(path, 'rt') as stream:
			data = stream.read()

		match = subst.search(data)
		if match:
			orig = match.group(0)
			span = match.span(1)
			if match.lastindex != 1:
				raise SafeException("Regex '%s' must have exactly one matching () group" % subst.pattern)
			assert span[0] >= 0, "Version match group did not match (regexp=%s; match=%s)" % (subst.pattern, orig)
			new_data = data[:span[0]] + new_version + data[span[1]:]
		else:
			raise SafeException("No matches for regex '%s' in '%s'" % (subst.pattern, path))

		with open(path, 'wt') as stream:
			stream.write(new_data)

def do_release(local_feed, options):
	if options.master_feed_file or options.archive_dir_public_url or options.archive_upload_command or options.master_feed_upload_command:
		print(legacy_warning)

	if options.master_feed_file:
		options.master_feed_file = os.path.abspath(options.master_feed_file)

	if not local_feed.feed_for:
		raise SafeException("Feed %s missing a <feed-for> element" % local_feed.local_path)

	status = support.Status()
	local_impl = support.get_singleton_impl(local_feed)

	local_impl_dir = local_impl.id
	assert os.path.isabs(local_impl_dir)
	local_impl_dir = os.path.realpath(local_impl_dir)
	assert os.path.isdir(local_impl_dir)
	if not local_feed.local_path.startswith(local_impl_dir + os.sep):
		raise SafeException("Local feed path '%s' does not start with '%s'" %
				(local_feed.local_path, local_impl_dir + os.sep))

	# From the impl directory to the feed
	# NOT relative to the archive root (in general)
	local_iface_rel_path = local_feed.local_path[len(local_impl_dir) + 1:]
	assert not local_iface_rel_path.startswith('/')
	assert os.path.isfile(os.path.join(local_impl_dir, local_iface_rel_path))

	phase_actions = {}
	for phase in valid_phases:
		phase_actions[phase] = []	# List of <release:action> elements

	version_substitutions = []

	add_toplevel_dir = None
	release_management = local_feed.get_metadata(XMLNS_RELEASE, 'management')
	if len(release_management) == 1:
		info("Found <release:management> element.")
		release_management = release_management[0]
		for x in release_management.childNodes:
			if x.uri == XMLNS_RELEASE and x.name == 'action':
				phase = x.getAttribute('phase')
				if phase not in valid_phases:
					raise SafeException("Invalid action phase '%s' in local feed %s. Valid actions are:\n%s" % (phase, local_feed.local_path, '\n'.join(valid_phases)))
				phase_actions[phase].append(x.content)
			elif x.uri == XMLNS_RELEASE and x.name == 'update-version':
				version_substitutions.append((x.getAttribute('path'), re.compile(x.content, re.MULTILINE)))
			elif x.uri == XMLNS_RELEASE and x.name == 'add-toplevel-directory':
				add_toplevel_dir = local_feed.get_name()
			else:
				warn("Unknown <release:management> element: %s", x)
	elif len(release_management) > 1:
		raise SafeException("Multiple <release:management> sections in %s!" % local_feed)
	else:
		info("No <release:management> element found in local feed.")

	scm = get_scm(local_feed, options)

	# Path relative to the archive / SCM root
	local_iface_rel_root_path = local_feed.local_path[len(scm.root_dir) + 1:]

	def run_hooks(phase, cwd, env):
		info("Running hooks for phase '%s'" % phase)
		full_env = os.environ.copy()
		full_env.update(env)
		for x in phase_actions[phase]:
			print "[%s]: %s" % (phase, x)
			support.check_call(x, shell = True, cwd = cwd, env = full_env)

	def set_to_release():
		print "Snapshot version is " + local_impl.get_version()
		release_version = options.release_version
		if release_version is None:
			suggested = support.suggest_release_version(local_impl.get_version())
			release_version = raw_input("Version number for new release [%s]: " % suggested)
			if not release_version:
				release_version = suggested

		scm.ensure_no_tag(release_version)

		status.head_before_release = scm.get_head_revision()
		status.save()

		working_copy = local_impl.id
		do_version_substitutions(local_impl_dir, version_substitutions, release_version)
		run_hooks('commit-release', cwd = working_copy, env = {'RELEASE_VERSION': release_version})

		print "Releasing version", release_version
		support.publish(local_feed.local_path, set_released = 'today', set_version = release_version)

		support.backup_if_exists(release_version)
		os.mkdir(release_version)
		os.chdir(release_version)

		status.old_snapshot_version = local_impl.get_version()
		status.release_version = release_version
		status.head_at_release = scm.commit('Release %s' % release_version, branch = TMP_BRANCH_NAME, parent = 'HEAD')
		status.save()

	def set_to_snapshot(snapshot_version):
		assert snapshot_version.endswith('-post')
		support.publish(local_feed.local_path, set_released = '', set_version = snapshot_version)
		do_version_substitutions(local_impl_dir, version_substitutions, snapshot_version)
		scm.commit('Start development series %s' % snapshot_version, branch = TMP_BRANCH_NAME, parent = TMP_BRANCH_NAME)
		status.new_snapshot_version = scm.get_head_revision()
		status.save()

	def ensure_ready_to_release():
		#if not options.master_feed_file:
		#	raise SafeException("Master feed file not set! Check your configuration")

		scm.ensure_committed()
		scm.ensure_versioned(os.path.abspath(local_feed.local_path))
		info("No uncommitted changes. Good.")
		# Not needed for GIT. For SCMs where tagging is expensive (e.g. svn) this might be useful.
		#run_unit_tests(local_impl)

		scm.grep('\(^\\|[^=]\)\<\\(TODO\\|XXX\\|FIXME\\)\>')

		branch = scm.get_current_branch()
		if branch != "refs/heads/master":
			print "\nWARNING: you are currently on the '%s' branch.\nThe release will be made from that branch.\n" % branch

	def create_feed(target_feed, local_iface_path, archive_file, archive_name, main):
		shutil.copyfile(local_iface_path, target_feed)

		support.publish(target_feed,
			set_main = main,
			archive_url = support.get_archive_url(options, status.release_version, os.path.basename(archive_file)),
			archive_file = archive_file,
			archive_extract = archive_name)

	def get_previous_release(this_version):
		"""Return the highest numbered verison in the master feed before this_version.
		@return: version, or None if there wasn't one"""
		parsed_release_version = model.parse_version(this_version)

		versions = [model.parse_version(version) for version in scm.get_tagged_versions()]
		versions = [version for version in versions if version < parsed_release_version]

		if versions:
			return model.format_version(max(versions))
		return None

	def export_changelog(previous_release):
		changelog = file('changelog-%s' % status.release_version, 'w')
		try:
			try:
				scm.export_changelog(previous_release, status.head_before_release, changelog)
			except SafeException, ex:
				print "WARNING: Failed to generate changelog: " + str(ex)
			else:
				print "Wrote changelog from %s to here as %s" % (previous_release or 'start', changelog.name)
		finally:
			changelog.close()

	def fail_candidate():
		cwd = os.getcwd()
		assert cwd.endswith(status.release_version)
		support.backup_if_exists(cwd)
		scm.delete_branch(TMP_BRANCH_NAME)
		os.unlink(support.release_status_file)
		print "Restored to state before starting release. Make your fixes and try again..."

	def release_via_0repo(new_impls_feed):
		import repo.cmd
		support.make_archives_relative(new_impls_feed)
		oldcwd = os.getcwd()
		try:
			repo.cmd.main(['0repo', 'add', '--', new_impls_feed])
		finally:
			os.chdir(oldcwd)

	def release_without_0repo(archive_file, new_impls_feed):
		assert options.master_feed_file

		if not options.archive_dir_public_url:
			raise SafeException("Archive directory public URL is not set! Edit configuration and try again.")

		if status.updated_master_feed:
			print "Already added to master feed. Not changing."
		else:
			publish_opts = {}
			if os.path.exists(options.master_feed_file):
				# Check we haven't already released this version
				master = support.load_feed(os.path.realpath(options.master_feed_file))
				existing_releases = [impl for impl in master.implementations.values() if impl.get_version() == status.release_version]
				if len(existing_releases):
					raise SafeException("Master feed %s already contains an implementation with version number %s!" % (options.master_feed_file, status.release_version))

				previous_release = get_previous_release(status.release_version)
				previous_testing_releases = [impl for impl in master.implementations.values() if impl.get_version() == previous_release
													     and impl.upstream_stability == model.stability_levels["testing"]]
				if previous_testing_releases:
					print "The previous release, version %s, is still marked as 'testing'. Set to stable?" % previous_release
					if support.get_choice(['Yes', 'No']) == 'Yes':
						publish_opts['select_version'] = previous_release
						publish_opts['set_stability'] = "stable"

			support.publish(options.master_feed_file, local = new_impls_feed, xmlsign = True, key = options.key, **publish_opts)

			status.updated_master_feed = 'true'
			status.save()

		# Copy files...
		uploads = [os.path.basename(archive_file)]
		for b in compiler.get_binary_feeds():
			binary_feed = support.load_feed(b)
			impl, = binary_feed.implementations.values()
			uploads.append(os.path.basename(impl.download_sources[0].url))

		upload_archives(options, status, uploads)

		feed_base = os.path.dirname(list(local_feed.feed_for)[0])
		feed_files = [options.master_feed_file]
		print "Upload %s into %s" % (', '.join(feed_files), feed_base)
		cmd = options.master_feed_upload_command.strip()
		if cmd:
			support.show_and_run(cmd, feed_files)
		else:
			print "NOTE: No feed upload command set => you'll have to upload them yourself!"

	def accept_and_publish(archive_file, src_feed_name):
		if status.tagged:
			print "Already tagged in SCM. Not re-tagging."
		else:
			scm.ensure_committed()
			head = scm.get_head_revision()
			if head != status.head_before_release:
				raise SafeException("Changes committed since we started!\n" +
						    "HEAD was " + status.head_before_release + "\n"
						    "HEAD now " + head)

			scm.tag(status.release_version, status.head_at_release)
			scm.reset_hard(TMP_BRANCH_NAME)
			scm.delete_branch(TMP_BRANCH_NAME)

			status.tagged = 'true'
			status.save()

		assert len(local_feed.feed_for) == 1

		# Merge the source and binary feeds together first, so
		# that we update the master feed atomically and only
		# have to sign it once.
		with open(src_feed_name, 'rb') as stream:
			doc = minidom.parse(stream)
		for b in compiler.get_binary_feeds():
			with open(b, 'rb') as stream:
				bin_doc = minidom.parse(b)
			merge.merge(doc, bin_doc)
		new_impls_feed = 'merged.xml'
		with open(new_impls_feed, 'wb') as stream:
			doc.writexml(stream)

		# TODO: support uploading to a sub-feed (requires support in 0repo too)
		master_feed, = local_feed.feed_for
		repository = registry.lookup(master_feed, missing_ok = True)
		if repository:
			release_via_0repo(new_impls_feed)
		else:
			release_without_0repo(archive_file, new_impls_feed)

		os.unlink(new_impls_feed)

		print "Push changes to public SCM repository..."
		public_repos = options.public_scm_repository
		if public_repos:
			scm.push_head_and_release(status.release_version)
		else:
			print "NOTE: No public repository set => you'll have to push the tag and trunk yourself."

		os.unlink(support.release_status_file)

	if status.head_before_release:
		head = scm.get_head_revision()
		if status.release_version:
			print "RESUMING release of %s %s" % (local_feed.get_name(), status.release_version)
			if options.release_version and options.release_version != status.release_version:
				raise SafeException("Can't start release of version %s; we are currently releasing %s.\nDelete the release-status file to abort the previous release." % (options.release_version, status.release_version))
		elif head == status.head_before_release:
			print "Restarting release of %s (HEAD revision has not changed)" % local_feed.get_name()
		else:
			raise SafeException("Something went wrong with the last run:\n" +
					    "HEAD revision for last run was " + status.head_before_release + "\n" +
					    "HEAD revision now is " + head + "\n" +
					    "You should revert your working copy to the previous head and try again.\n" +
					    "If you're sure you want to release from the current head, delete '" + support.release_status_file + "'")
	else:
		print "Releasing", local_feed.get_name()

	ensure_ready_to_release()

	if status.release_version:
		if not os.path.isdir(status.release_version):
			raise SafeException("Can't resume; directory %s missing. Try deleting '%s'." % (status.release_version, support.release_status_file))
		os.chdir(status.release_version)
		need_set_snapshot = False
		if status.tagged:
			print "Already tagged. Resuming the publishing process..."
		elif status.new_snapshot_version:
			head = scm.get_head_revision()
			if head != status.head_before_release:
				raise SafeException("There are more commits since we started!\n"
						    "HEAD was " + status.head_before_release + "\n"
						    "HEAD now " + head + "\n"
						    "To include them, delete '" + support.release_status_file + "' and try again.\n"
						    "To leave them out, put them on a new branch and reset HEAD to the release version.")
		else:
			raise SafeException("Something went wrong previously when setting the new snapshot version.\n" +
					    "Suggest you reset to the original HEAD of\n%s and delete '%s'." % (status.head_before_release, support.release_status_file))
	else:
		set_to_release()	# Changes directory
		assert status.release_version
		need_set_snapshot = True

	# May be needed by the upload command
	os.environ['RELEASE_VERSION'] = status.release_version

	archive_name = support.make_archive_name(local_feed.get_name(), status.release_version)
	archive_file = archive_name + '.tar.bz2'

	export_prefix = archive_name
	if add_toplevel_dir is not None:
		export_prefix += os.sep + add_toplevel_dir

	if status.created_archive and os.path.isfile(archive_file):
		print "Archive already created"
	else:
		support.backup_if_exists(archive_file)
		scm.export(export_prefix, archive_file, status.head_at_release)

		has_submodules = scm.has_submodules()

		if phase_actions['generate-archive'] or has_submodules:
			try:
				support.unpack_tarball(archive_file)
				if has_submodules:
					scm.export_submodules(archive_name)
				run_hooks('generate-archive', cwd = archive_name, env = {'RELEASE_VERSION': status.release_version})
				info("Regenerating archive (may have been modified by generate-archive hooks...")
				support.check_call(['tar', 'cjf', archive_file, archive_name])
			except SafeException:
				scm.reset_hard(scm.get_current_branch())
				fail_candidate()
				raise

		status.created_archive = 'true'
		status.save()

	if need_set_snapshot:
		set_to_snapshot(status.release_version + '-post')
		# Revert back to the original revision, so that any fixes the user makes
		# will get applied before the tag
		scm.reset_hard(scm.get_current_branch())

	#backup_if_exists(archive_name)
	support.unpack_tarball(archive_file)

	extracted_feed_path = os.path.abspath(os.path.join(export_prefix, local_iface_rel_root_path))
	assert os.path.isfile(extracted_feed_path), "Local feed not in archive! Is it under version control?"
	extracted_feed = support.load_feed(extracted_feed_path)
	extracted_impl = support.get_singleton_impl(extracted_feed)

	if extracted_impl.main:
		# Find main executable, relative to the archive root
		abs_main = os.path.join(os.path.dirname(extracted_feed_path), extracted_impl.id, extracted_impl.main)
		main = os.path.relpath(abs_main, archive_name + os.sep)
		if main != extracted_impl.main:
			print "(adjusting main: '%s' for the feed inside the archive, '%s' externally)" % (extracted_impl.main, main)
			# XXX: this is going to fail if the feed uses the new <command> syntax
		if not os.path.exists(abs_main):
			raise SafeException("Main executable '%s' not found after unpacking archive!" % abs_main)
		if main == extracted_impl.main:
			main = None	# Don't change the main attribute
	else:
		main = None

	try:
		if status.src_tests_passed:
			print "Unit-tests already passed - not running again"
		else:
			# Make directories read-only (checks tests don't write)
			support.make_readonly_recursive(archive_name)

			run_unit_tests(extracted_feed_path)
			status.src_tests_passed = True
			status.save()
	except SafeException:
		print "(leaving extracted directory for examination)"
		fail_candidate()
		raise
	# Unpack it again in case the unit-tests changed anything
	ro_rmtree(archive_name)
	support.unpack_tarball(archive_file)

	# Generate feed for source
	src_feed_name = '%s.xml' % archive_name
	create_feed(src_feed_name, extracted_feed_path, archive_file, archive_name, main)
	print "Wrote source feed as %s" % src_feed_name

	# If it's a source package, compile the binaries now...
	compiler = compile.Compiler(options, os.path.abspath(src_feed_name), release_version = status.release_version)
	compiler.build_binaries()

	previous_release = get_previous_release(status.release_version)
	export_changelog(previous_release)

	if status.tagged:
		raw_input('Already tagged. Press Return to resume publishing process...')
		choice = 'Publish'
	else:
		print "\nCandidate release archive:", archive_file
		print "(extracted to %s for inspection)" % os.path.abspath(archive_name)

		print "\nPlease check candidate and select an action:"
		print "P) Publish candidate (accept)"
		print "F) Fail candidate (delete release-status file)"
		if previous_release:
			print "D) Diff against release archive for %s" % previous_release
			maybe_diff = ['Diff']
		else:
			maybe_diff = []
		print "(you can also hit CTRL-C and resume this script when done)"

		while True:
			choice = support.get_choice(['Publish', 'Fail'] + maybe_diff)
			if choice == 'Diff':
				previous_archive_name = support.make_archive_name(local_feed.get_name(), previous_release)
				previous_archive_file = '..' + os.sep + previous_release + os.sep + previous_archive_name + '.tar.bz2'

				# For archives created by older versions of 0release
				if not os.path.isfile(previous_archive_file):
					old_previous_archive_file = '..' + os.sep + previous_archive_name + '.tar.bz2'
					if os.path.isfile(old_previous_archive_file):
						previous_archive_file = old_previous_archive_file

				if os.path.isfile(previous_archive_file):
					support.unpack_tarball(previous_archive_file)
					try:
						support.show_diff(previous_archive_name, archive_name)
					finally:
						shutil.rmtree(previous_archive_name)
				else:
					# TODO: download it?
					print "Sorry, archive file %s not found! Can't show diff." % previous_archive_file
			else:
				break

	info("Deleting extracted archive %s", archive_name)
	shutil.rmtree(archive_name)

	if choice == 'Publish':
		accept_and_publish(archive_file, src_feed_name)
	else:
		assert choice == 'Fail'
		fail_candidate()
