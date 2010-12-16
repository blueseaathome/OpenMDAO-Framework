"""
Support for Role-Based Access Control (RBAC).

Remote access to methods is determined by a role-based access control attribute
attached to the method. This attribute contains a sequence of role patterns, an
optional proxy role to be used while executing the method, and a list of return
types which are to be proxied. The :class:`rbac` decorator is available for
assigning this access control attribute to methods.

Remote access to attributes is checked based on role, accessing method, object,
and attribute.

These access checks are mediated by an :class:`AccessController`. There is a
default controller assigned to each :class:`OpenMDAO_Server`. The server will
check for an object-specific controller by trying to invoke
:meth:`get_access_controller` on the object before using the default.

The current role is determined from a :class:`Credentials` object which is
attached to the current thread. Credentials are currently just a user
identifier string. Mapping from credentials to roles can become fairly involved
in a real system, typically with site-specific configuration. The default role
mapping here just returns the roles 'owner' or 'user' based on whether the
credentials match those in effect when the controller object was created.

.. warning::

    Credentials as currently defined are quite weak unless the receiver has
    a list of known client keys.  This access control scheme should *not* be
    relied upon unless the receiving server verifies that the Credentials
    public key matches what is expected.

"""

import fnmatch
import getpass
import hashlib
import inspect
import socket
import sys
import threading

from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes

from openmdao.util.publickey import generate_key_pair, HAVE_PYWIN32

# Verified credentials keyed by encoding tuple.
_VERIFY_CACHE = {}


class CredentialsError(Exception):
    """ Raised when decoding/verifying received credentials. """
    pass


class RoleError(Exception):
    """
    Raised when the current role is not allowed to invoke the requested
    method. Also raised when encountering some internal problems.
    """
    pass


class Credentials(object):
    """
    Credentials are used to certify that a message is from a particular user
    on a particular host.  The scheme here is quite weak unless the receiver
    has a list of known client keys.

    Essentially all we can prove here is that *someone* *somewhere* created
    a correctly formed credential. Without known client keys at the receiving
    end the best we can do is detect when more than one client claims the
    same identity.

    If the receiver does keep a list of known client keys, then the information
    here will support strict authorization.
    """

    user_host = '%s@%s' % (getpass.getuser(), socket.gethostname())

    def __init__(self, data=None, signature=None):
        # We don't use cPickle here because we'd rather not have to trust
        # the sender until after we've checked their credentials.

        if data is None and signature is None:
            # Create our credentials.
            self.user = Credentials.user_host
            self.transient = (sys.platform == 'win32') and not HAVE_PYWIN32
            key_pair = generate_key_pair(self.user)
            self.public_key = key_pair.publickey()
            self.data = '\n'.join([self.user, str(int(self.transient)),
                                   self.public_key.exportKey()])
            hash = hashlib.sha256(self.data).digest()
            self.signature = key_pair.sign(hash, get_random_bytes)
        else:
            # Recreate remote user credentials.
            lines = data.split('\n')
            if len(lines) < 3:
                raise CredentialsError('Invalid data')
            self.user = lines[0]
            self.transient = bool(int(lines[1]))
            try:
                self.public_key = RSA.importKey('\n'.join(lines[2:]))
            except Exception:
                raise CredentialsError('Invalid key')
            self.data = data
            hash = hashlib.sha256(data).digest()
            try:
                if not self.public_key.verify(hash, signature):
                    raise CredentialsError('Invalid signature')
            except Exception:
                raise CredentialsError('Invalid signature')
            self.signature = signature

    def __eq__(self, other):
        if isinstance(other, Credentials):
            return self.user == other.user and \
                   self.signature == other.signature
        else:
            return False

    def __str__(self):
        # 'transient' is just an aid to let some Windows users know their
        # credentials will change on-the-fly.
        transient = ' (transient)' if self.transient else ''
        return '%s%s' % (self.user, transient)

    def encode(self):
        """ Return object to be sent: ``(data, signature)``. """
        return (self.data, self.signature)

    @staticmethod
    def decode(encoded):
        """
        Return :class:`Credentials` object from `encoded`.
        The :attr:`public_key` should be checked against the expected value
        if it is available.

        encoded: tuple.
            Data received, returned by (remote) :meth:`encode`.
        """
        data, signature = encoded
        return Credentials(data, signature)

    @staticmethod
    def verify(encoded):
        """
        Verify that `encoded` is a valid encoded credentials object and that
        its public key matches the public key we've already seen, if any.

        Returns :class:`Credentials` object from `encoded`.
        """
        try:
            return _VERIFY_CACHE[encoded]
        except KeyError:
            credentials = Credentials.decode(encoded)
            user = credentials.user
            for cred in _VERIFY_CACHE.values():
                if cred.user == user:
                    raise CredentialsError('Public key mismatch for %r' % user)
            else:
                _VERIFY_CACHE[encoded] = credentials
            return credentials


def set_credentials(credentials):
    """ Set the current thread's credentials. """
    threading.current_thread().credentials = credentials
    return credentials

def get_credentials():
    """ Get the current thread's credentials. """
    try:
        return threading.current_thread().credentials
    except AttributeError:
        credentials = Credentials()
        return set_credentials(credentials)


# For some reason use of a class as a decorator doesn't count as coverage.
class rbac(object):  #pragma no cover
    """
    Decorator for specifying RBAC roles for a method.

    roles: string or sequence[string]
        Role name patterns which are allowed access.

    proxy_role: string
        Role to use during execution of method.
        A null string implies that the current role is used.

    proxy_types: list[class]
        Types of return values that must be proxied.

    """

    def __init__(self, roles, proxy_role='', proxy_types=None):
        self.roles = (roles,) if isinstance(roles, basestring) else tuple(roles)
        self.proxy_role = proxy_role
        self.proxy_types = () if proxy_types is None else tuple(proxy_types)

    def __call__(self, func):
        func._rbac = (self.roles, self.proxy_role, self.proxy_types, {})
        return func


def rbac_methods(obj):
    """
    Returns a list of names of the methods of `obj` to be exposed.

    obj: object
        Object to be scanned.

    """
    methods = []
    for name in dir(obj):
        attr = getattr(obj, name)
        if inspect.ismethod(attr):
            if hasattr(attr.__func__, '_rbac'):
                methods.append(name)
    return methods


def need_proxy(meth, result):
    """
    Returns True if `result` from `meth` requires a proxy.

    meth: method.
        Method to be checked.

    result: object
        Result value to be checked.

    """
    try:
        roles, proxy_role, types, cache = meth._rbac
    except AttributeError:
        return False

    if not types:
        return False

    cls = result.__class__
    try:
        return cache[cls]
    except KeyError:
        # Check if this result class or any base classes are in types.
        cache[cls] = isinstance(result, types)
        return cache[cls]


class AccessController(object):
    """
    Responsible for mapping :class:`Credentials` to roles and optionally
    getting different credentials for executing a method.

    Also reponsible for determining which attributes and classes require
    a proxy to be returned rather than the value.
    """

    def __init__(self):
        self.owner = get_credentials()
        self.credentials_map = {}
        self.set_proxy_credentials('owner', self.owner)
        self.attr_proxy_map = {}
        self.proxy_types = []

    def get_role(self, credentials):
        """
        Trivial :class:`Credentials`-to-role mapping.

        credentials: :class:`Credentials`
            Credentials to obtain role for.

        Returns the role associated with `credentials`.

        This version returns a null string for no credentials, 'owner' if
        `credentials` matches the credentials in effect when we were created,
        and 'user' otherwise.
        """
        if credentials is None:
            return ''  # Should be allowed for *very* few methods!
        if isinstance(credentials, Credentials):
            if credentials == self.owner:
                return 'owner'
            return 'user'
        else:
            raise TypeError('credentials is not a Credentials object')

    def get_proxy_credentials(self, meth, credentials):
        """
        If special credentials are needed while executing `meth`, return
        them, else return `credentials`.

        meth: method
            Method to be invoked.

        credentials: :class:`Credentials`
            Current credentials in effect.

        """
        try:
            proxy_role = meth._rbac[1]
        except AttributeError:
            raise RoleError('No RBAC for method %s' % meth)
        
        if proxy_role:
            try:
                return self.credentials_map[proxy_role]
            except KeyError:
                raise RoleError('No credentials for proxy role %s' % proxy_role)
        else:
            return credentials

    def set_proxy_credentials(self, proxy_role, credentials):
        """
        Set credentials to be used for `proxy_role` to `credentials`.

        proxy_role: string
            Role to assign credentials to.

        credentials: :class:`Credentials`
            Credentials to be assigned.

        """
        if isinstance(credentials, Credentials):
            self.credentials_map[proxy_role] = credentials
        else:
            raise TypeError('credentials is not a Credentials object')

    def check_access(self, role, methodname, obj, attr):
        """
        Verify that `role` is allowed to invoke `methodname` of `obj` for
        `attr`.

        role: string
            Current role.

        methodname: string
            Name of method to be invoked.

        obj: object
            Object whose attribute is to be accessed.

        attr: string
            Name of attribute to be accessed.

        Used for access control on :func:`getattr`, :func:`setattr`,
        and :func:`delattr` operations. This default version requires a non-null
        role, and only 'owner' may delete attributes.
        """
        if not role:
            raise RoleError('No access by null role')
        if methodname != '__delattr__':
            return
        if role != 'owner':
            raise RoleError("No %s access to '%s' by role '%s'"
                            % (methodname, attr, role))

    def need_proxy(self, obj, attr, res):
        """
        Returns True if `attr` of `obj` whose value is `res` requires a proxy.

        obj: object
            Object whose attribute is to be returned.

        attr: string
            Name of attribute accessed.

        res: object
            Result to be returned.

        Checks `res` against registered classes to be proxied as well as
        the proxy registry for `obj.attr`.
        """
        if isinstance(res, tuple(self.proxy_types)):
            return True

        key = '%s.%s' % (id(obj), attr)
        try:
            return self.attr_proxy_map[key]
        except KeyError:
            return False

    def attr_proxy_required(self, obj, attr, required=True):
        """
        Record that a proxy is/is not required for `obj.attr`.

        obj: object
            Object whose attribute is to be recorded.

        attr: string
            Name of attribute to be recorded.

        required: bool
            If True, a proxy must be created.

        """
        key = '%s.%s' % (id(obj), attr)
        self.attr_proxy_map[key] = required

    def class_proxy_required(self, cls):
        """
        Record that a proxy is required for `cls` or any subclasses.

        cls: class
            Class to be recorded.

        """
        self.proxy_types.append(cls)


def check_role(role, meth):
    """
    Verifies that `role` is matched by at least one :mod:`fnmatch`-style
    pattern in `meth`'s RBAC. Raises :class:`RoleError` if no match is found.

    role: string
        Role to be checked.

    meth: method.
        Method to be checked.

    """
    try:
        patterns = meth._rbac[0]
    except AttributeError:
        raise RoleError('No RBAC for function!')

    for pattern in patterns:
        if fnmatch.fnmatchcase(role, pattern):
            return
    raise RoleError("No access for role '%s'" % role)

