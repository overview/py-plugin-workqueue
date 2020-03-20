overview-plugin-workqueue
=========================

In-process work queue for use in Overview plugins.

The problem it solves
---------------------

You're writing an `Overview plugin
<https://github.com/overview/overview-server/wiki/Writing-a-Plugin>`_.
It's a web server. And when a user creates a View of your plugin, you want
to kick off some long-running computation.

``overview-plugin-workqueue`` will solve this problem in a "prototype"
or lightweight situation. It's meant to be used on a single Python server.

Your plugin's architecture
--------------------------

Your plugin should be a web server with four endpoints:

* ``GET /metadata``: the usual metadata
* ``GET /show``: present HTML with JavaScript web app. The JavaScript will call:
* ``GET /search?server=...&documentSetId=...`` (or some-such): read ``Authorization`` header with ``api_token``; generates some JSON using a locally-stored "database".

But this "database" -- specific to the ``apiToken`` on this ``server`` -- must exist! So we need:

* ``POST /generate``: accept form data with ``server`` and ``document_set_id``
  parameters, plus an ``Authorization`` header (or ``api_token`` parameter);
  generates the database for this view.

The Python side of things
~~~~~~~~~~~~~~~~~~~~~~~~~

Your plugin is a web server. Remember: Web servers are stateless.

Your plugin should save and read a "database" file whose name is derived from
``server``, ``document_set_id`` and ``api_token``. Don't worry -- ``workqueue``
will name the file for you.

You must anticipate spurious ``/generate`` calls, and you must give progress
reports to your users or they'll assume something's wrong. You also want jobs
to continue even if your users go away; and when they reconnect, progress
should resume where it left off.

This is where ``workqueue`` shines. It uses an in-process thread pool to limit
the number of concurrent calls, and it uses generators to keep the server
responsive even when serving long-polling progress-report requests.

Install it: ``pip3 install workqueue``

Now create a database-building script *as a separate executable*. For instance,
``process-docset.py`` (``chmod +x`` it, and make sure it begins with
``#!/usr/bin/env python3``.) ``workqueue`` will invoke it like this::

    ./process-docset.py SERVER DOCUMENT_SET_ID API_TOKEN OUTPUT_FILENAME

And ``workqueue`` will provide these arguments:

* ``SERVER``: Overview server -- e.g., ``https://www.overviewdocs.com``.
* ``DOCUMENT_SET_ID``: String ID -- e.g., ``1234``.
* ``API_TOKEN``: An alphanumeric API token.
* ``OUTPUT_FILENAME``: An empty tempfile the script must overwrite.

Test your script by running it on the command line.

Now you're ready to use ``workqueue`` in your plugin web server. For example,
here it is in `Flask <https://flask.palletsprojects.com/en/1.1.x/>`_::

    from pathlib import Path
    from typing import Mapping

    from werkzeug.wrappers import Response
    from workqueue import WorkQueue, OverviewViewParams

    # ... set `app` ... and then

    WORK_QUEUE = WorkQueue(
            Path(__file__).parent / "process-docset.py",
            executor=ThreadPoolExecutor(2, "process-docset-"),
            storage_dir=Path(__file__).parent / "databases",
    )

    def _extract_api_token(auth_header: str) -> str:
        m = _AUTH_HEADER_REGEX.match(auth_header)
        if not m:
            raise ValueError(
                'Authorization header must look like "Basic [base64-encoded data]"'
            )
        decoded = base64.b64decode(m.group(1))
        m = _AUTH_TOKEN_STRING_REGEX.match(decoded)
        if not m:
            raise ValueError(
                'base64-encoded Basic HTTP Auth value must look like "[api_token]:x-api-token"'
            )
        return m.group(1).decode("latin1")


    def _extract_params(
        form: Mapping[str, str], headers: Mapping[str, str]
    ) -> OverviewViewParams:
        try:
            return OverviewViewParams(
                form["server"],
                form["documentSetId"],
                _extract_api_token(headers["Authorization"]),
            )
        except (KeyError, ValueError) as err:
            raise BadRequest(
                "Expected form data ?server=...&documentSetId=... and Authorization header. "
                "Problem: %s" % str(err)
            )


    @app.route("/generate", methods=["POST"])
    def generate():
        """
        Stream a JSON Array of "progress" events, as a long-polled response.

        When the JSON stream is finished, the database is ready.

        This method returns early, passing Flask a generator. Flask will consume
        the chunks from the generator and send them to the client.
        """
        overview_params = _extract_params(request.form, request.headers)

        job = WORK_QUEUE.ensure_run(overview_params)
        if job is None:
            # The work is done; the model can be loaded now.
            progress_stream = []
        else:
            progress_stream = WORK_QUEUE.report_job_progress_until_completed(job)

        # Stream progress to the user, as a JSON Array. (Must not be buffered.)
        json_stream = itertools.chain(
            ["["],
            (
                # JSON array: comma between elements, not before first or after last
                ("" if i == 0 else ",") + json.dumps(progress._asdict())
                for i, progress in enumerate(progress_stream)
            ),
            ["]"],
        )

        return Response(json_stream, content_type="application/json")


The JavaScript side of things
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On app startup, ``POST`` to ``/generate`` using `Oboe <http://oboejs.com/api>`_.
Pass it ``server`` and ``documentSetId``, and pass an ``Authorization`` header
(or ``apiToken`` form parameter) as well.

Use Oboe, not a normal ``XMLHttpRequest`` or ``Fetch`` request, because Oboe
will notify about progress events before ``/generate`` completes. (And
``/generate`` could take a very long time indeed to complete.)

Once ``/generate`` completes, you can invoke ``/query`` using the same
server, documentSetId and apiToken.

Scaling limits
==============

``workqueue`` is designed to be used in relatively-small deployments. It's a
great place to start. Here are the parts that will limit you:

* **storage**: ``workqueue`` is designed to read and write the local disk.
  TODO read and write S3-compatible storage servers.
* **single-server**: ``workqueue`` cannot distribute load across multiple
  machines. Use a different process to achieve that.

Developing This Package
=======================

Install dependency: ``pip3 install --user tox``

Run ``tox`` to make sure it works for you.

Then the development loop:

1. Write a test. Run ``tox`` and prove to yourself it fails.
2. Write code. Run ``tox`` to prove it solves your problem.
3. Submit a pull request.

Release Process
---------------

1. Edit the ``version=`` line in ``setup.py``. Adhere to semver.
2. ``git commit setup.py -m "vX.X.X" && git push``
3. ``python ./setup.py sdist && twine check dist/* && twine upload dist/*-X.X.X.tar.gz``

License
=======

MIT. See ``LICENSE``.
